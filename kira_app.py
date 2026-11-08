"""
KIRA LAUNCHER - Arquivo Ãºnico que inicia tudo
=============================================
Inicia: Interface web + Agente IA + Janela nativa + Bandeja do sistema

Instalar dependÃªncias (uma vez sÃ³):
    pip install pywebview pystray pillow fastapi uvicorn python-dotenv livekit-api httpx

Rodar:
    python kira_app.py
"""

import asyncio
import ctypes
import os
import sys
import time
import random
import signal
import threading
import subprocess
import json
import re
import logging
import socket
import secrets
import shutil
import tempfile
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parent
_project_root_str = str(PROJECT_ROOT)
try:
    sys.path.remove(_project_root_str)
except ValueError:
    pass
sys.path.insert(0, _project_root_str)

import kira_httpx_ssl_cache  # noqa: E402,F401  (cliente httpx novo travava o loop ~400 ms)

# Some test flows import agent modules first, which can preload another top-level
# "services" package from agent/services. Force the root launcher to resolve the
# root services package before importing runtime dependencies.
_loaded_services_pkg = sys.modules.get("services")
if _loaded_services_pkg is not None:
    _loaded_services_file = getattr(_loaded_services_pkg, "__file__", "") or ""
    try:
        _loaded_services_path = Path(_loaded_services_file).resolve()
    except Exception:
        _loaded_services_path = None
    _expected_services_dir = PROJECT_ROOT / "services"
    if _loaded_services_path is None or _expected_services_dir not in _loaded_services_path.parents:
        for _module_name in list(sys.modules):
            if _module_name == "services" or _module_name.startswith("services."):
                sys.modules.pop(_module_name, None)

from dotenv import load_dotenv
from services.agent_restart_policy import AgentRestartPolicy
from services.livekit_key_manager import (
    LiveKitCredential,
    LiveKitKeyManager,
    is_livekit_auth_error,
    is_livekit_quota_error,
)
from services.livekit_provider_manager import (
    LiveKitProvider,
    LiveKitProviderError,
    apply_provider_to_env,
    build_livekit_provider_status,
    get_selected_provider,
    is_cloud_provider,
    is_local_provider,
    mask_provider_for_logs,
    select_livekit_provider,
)
from services.transcription_bus import transcription_bus
from services.session_recap import get_session_recap
from services.interests import note_user_text
from services.dialogue_log import get_dialogue_log_writer
from services.runtime_status import get_runtime_status_manager, RuntimePhase, VoiceStatus
from services.economy_mode_manager import get_economy_mode_manager
from services.session_transport_manager import (
    cleanup_transport_lock_if_safe,
    SessionTransportSwitchManager,
    TransportDecision,
    TransportSwitchResult,
    late_transport_failure_reason,
    provider_kind_from_snapshot,
    recovery_to_cloud_allowed,
    runtime_ready_for_transport,
    transition_is_stale,
    transport_block_reasons,
)
from services.api_identity_snapshot import (
    get_api_identity_snapshot,
    update_api_identity_snapshot,
)
from services.kira_feature_flags import (
    get_optional_modules,
    is_optional_enabled,
    load_kira_features,
    optional_status_payload,
    should_autoload,
)
from services.transcription_queue import get_transcription_queue
from boot_manager import BootPhase, boot_orchestrator
from services.boot_guard import (
    get_degraded_services,
    optional_endpoint_error,
    record_degraded_service,
    run_optional_task,
    safe_import_optional,
)
from services.kira_maintenance_registry import collect_maintenance_snapshot
from services.session_timing import record_timing, startup_timing_mark
from services.kira_ui_flags import desktop_ui_query
from services.subprocess_health import inspect_processes
from services.artifact_registry import (
    get_artifact_by_id,
    get_job,
    get_jobs_summary,
    get_last_artifact,
    is_registered_artifact_path,
    list_recent_artifacts,
    list_recent_jobs,
    reconcile_jobs,
)
from services.local_file_actions import open_file, open_folder
from services.artifact_event_bus import artifact_event_bus
from agent.core.personality_response import (
    build_personality_response,
    format_browser_ack,
    format_ui_panel_ack,
    format_volume_ack,
    get_kira_style_rules,
)

# â”€â”€ Controle da janela do CMD (Windows) â”€â”€
import ctypes
from assistant_desktop_bridge import AssistantDesktopBridge, AssistantDesktopBridgeError

logger = logging.getLogger("kira.app")
_stdout_watcher_unicode_replaced_logged = False
if not logger.handlers:
    _kira_app_log_handler = logging.StreamHandler(sys.stdout)
    _kira_app_log_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_kira_app_log_handler)


# HOTFIX UI/CPU 2: cache curto e rate-limit apenas para endpoints/logs de status.
# Nao altera endpoints mutaveis, boot/session start, voz, LiveKit ou Gemini.
class _StatusResponseCache:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: str, ttl_seconds: float, compute_fn):
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item and now < float(item.get("expires_at", 0.0)):
                _rate_limited_log(
                    f"status_cache_hit:{key}",
                    "STATUS_CACHE_HIT endpoint=%s",
                    key,
                    interval_s=30.0,
                )
                return item.get("value")
        value = compute_fn()
        if isinstance(value, dict) and (value.get("fatal_error") or value.get("session_failed")):
            _rate_limited_log("status_cache_bypass:fatal", "STATUS_CACHE_BYPASS reason=fatal_state endpoint=%s", key, interval_s=5.0)
            return value
        with self._lock:
            self._items[key] = {"value": value, "expires_at": now + ttl_seconds}
        _rate_limited_log(f"status_cache_miss:{key}", "STATUS_CACHE_MISS endpoint=%s", key, interval_s=30.0)
        return value

    async def get_or_compute_async(self, key: str, ttl_seconds: float, compute_fn):
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item and now < float(item.get("expires_at", 0.0)):
                _rate_limited_log(
                    f"status_cache_hit:{key}",
                    "STATUS_CACHE_HIT endpoint=%s",
                    key,
                    interval_s=30.0,
                )
                return item.get("value")
        value = await compute_fn()
        if isinstance(value, dict) and (value.get("fatal_error") or value.get("session_failed")):
            _rate_limited_log("status_cache_bypass:fatal", "STATUS_CACHE_BYPASS reason=fatal_state endpoint=%s", key, interval_s=5.0)
            return value
        with self._lock:
            self._items[key] = {"value": value, "expires_at": now + ttl_seconds}
        _rate_limited_log(f"status_cache_miss:{key}", "STATUS_CACHE_MISS endpoint=%s", key, interval_s=30.0)
        return value

    def invalidate(self, reason: str = "state_changed") -> None:
        with self._lock:
            self._items.clear()
        _rate_limited_log("status_cache_invalidated", "STATUS_CACHE_INVALIDATED reason=%s", reason, interval_s=10.0)


_status_response_cache = _StatusResponseCache()
_rate_limited_log_state: dict[str, dict[str, object]] = {}
_rate_limited_log_lock = threading.Lock()


def _rate_limited_log(key: str, message: str, *args, interval_s: float = 30.0, level: int = logging.INFO) -> None:
    now = time.monotonic()
    skipped = 0
    should_log = False
    with _rate_limited_log_lock:
        item = _rate_limited_log_state.get(key)
        if item is None:
            _rate_limited_log_state[key] = {"last": now, "skipped": 0}
            should_log = True
        else:
            last = float(item.get("last", 0.0))
            if now - last >= interval_s:
                skipped = int(item.get("skipped", 0) or 0)
                item["last"] = now
                item["skipped"] = 0
                should_log = True
            else:
                item["skipped"] = int(item.get("skipped", 0) or 0) + 1
    if should_log:
        logger.log(level, message, *args)
        if skipped:
            logger.info("LOG_RATE_LIMITED event=%s skipped=%s", key, skipped)


def _write_stdout_watcher_line_safe(line: str, stream=None) -> None:
    global _stdout_watcher_unicode_replaced_logged
    output = stream if stream is not None else sys.stdout
    encoding = str(getattr(output, "encoding", "") or "utf-8")
    raw_line = str(line or "")
    safe_line = raw_line.encode(encoding, errors="replace").decode(encoding, errors="replace")
    output.write(safe_line)
    output.flush()
    if safe_line != raw_line and not _stdout_watcher_unicode_replaced_logged:
        _stdout_watcher_unicode_replaced_logged = True
        logger.warning("STDOUT_WATCHER_UNICODE_REPLACED")
logger.setLevel(logging.INFO)
logger.propagate = False

_TRANSCRIPTION_DEDUPE_WINDOW_SECONDS = 3.0
_transcription_dedupe_state: dict[str, dict[str, object]] = {}
_transcription_recent_events: list[dict[str, object]] = []


def _normalize_transcript_text_for_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return normalized


def _build_transcription_dedupe_key(source: str, text: str, payload: dict) -> tuple[str, str]:
    message_id = str(payload.get("message_id") or "").strip()
    reply_id = str(payload.get("reply_id") or "").strip()
    request_id = str(payload.get("request_id") or "").strip()
    turn_id = str(payload.get("turn_id") or "").strip()
    if message_id:
        return f"message:{message_id}", "message_id"
    if reply_id:
        return f"reply:{reply_id}", "reply_id"
    if request_id:
        return f"request:{request_id}", "request_id"
    if turn_id:
        return f"turn:{turn_id}", "turn_id"
    normalized = _normalize_transcript_text_for_fingerprint(text)
    return f"fp:{source}:{normalized}", "fingerprint"

def _normalize_ready_probe_url(raw_url: str | None, default: str = "http://127.0.0.1:8081/") -> str:
    candidate = str(raw_url or "").strip() or default
    parsed = urlsplit(candidate)
    scheme = str(parsed.scheme or "http").strip() or "http"
    netloc = str(parsed.netloc or "").strip()
    path = str(parsed.path or "").strip()

    if not netloc and path:
        netloc = path
        path = "/"

    if not netloc:
        return default

    normalized_path = path or "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    return urlunsplit((scheme, netloc, normalized_path, parsed.query, parsed.fragment))


LIVEKIT_SERVER_PATH = PROJECT_ROOT / "livekit" / "livekit-server.exe"
LIVEKIT_PORT = 7880
LIVEKIT_WORKER_HTTP_URL = _normalize_ready_probe_url(
    os.getenv("KIRA_AGENT_READY_URL", "http://127.0.0.1:8081/")
)
WORKER_HTTP_READY_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_AGENT_READY_TIMEOUT_SECONDS", "90")),
    10.0,
)
MEMORY_HEALTH_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_MEMORY_HEALTH_TIMEOUT_SECONDS", "12")),
    3.0,
)
MEMORY_BOOT_CONTEXT_PATH = PROJECT_ROOT / "runtime" / "memory_boot_context.json"
_livekit_process = None
_livekit_started_here = False
LIVEKIT_SELECTED_PROVIDER: LiveKitProvider | None = None
_economy_transport_switch_manager = SessionTransportSwitchManager()
_economy_transport_switch_lock = threading.Lock()
_economy_pending_ui_reconnect_lock = threading.RLock()
_economy_pending_ui_reconnect: dict[str, object] | None = None
_economy_active_transport_switch_lock = threading.RLock()
_economy_active_transport_switch: dict[str, object] | None = None
_economy_transport_monitor_threads: set[str] = set()


def _select_and_apply_livekit_provider(*, force_refresh: bool = False) -> LiveKitProvider:
    global LIVEKIT_SELECTED_PROVIDER
    if LIVEKIT_SELECTED_PROVIDER is not None and not force_refresh:
        return LIVEKIT_SELECTED_PROVIDER
    boot_mode = None  # Configurações > Modo econômico: LiveKit da próxima inicialização (local/cloud/auto)
    if not str(os.environ.get("LIVEKIT_PROVIDER") or "").strip():
        try:
            boot_mode = str(get_economy_mode_manager().get_config().get("next_boot_livekit") or "auto")
        except Exception:
            boot_mode = None
    try:
        provider = select_livekit_provider(mode=boot_mode, validate_cloud=boot_mode != "local")
    except LiveKitProviderError as exc:
        logger.error("LIVEKIT_PROVIDER_SELECTION_FAILED error=%s", exc)
        raise
    apply_provider_to_env(provider)
    LIVEKIT_SELECTED_PROVIDER = provider
    try:
        update_api_identity_snapshot(provider=provider)
    except Exception as exc:
        logger.warning("API_IDENTITY_SNAPSHOT_FAILED_NON_FATAL service=livekit reason=%s", exc.__class__.__name__)
    logger.info(
        "LIVEKIT_PROVIDER_RUNTIME_APPLIED provider=%s",
        json.dumps(mask_provider_for_logs(provider), ensure_ascii=False),
    )
    return provider


def _selected_livekit_provider() -> LiveKitProvider:
    return LIVEKIT_SELECTED_PROVIDER or _select_and_apply_livekit_provider()


def _apply_livekit_provider_for_transport_switch(target_kind: str) -> LiveKitProvider:
    """Aplica provider cloud/local em runtime e recria credenciais do worker."""
    global LIVEKIT_SELECTED_PROVIDER, LIVEKIT_MANAGER
    normalized = str(target_kind or "").strip().lower()
    if normalized not in {"cloud", "local"}:
        raise ValueError(f"unsupported_provider:{target_kind}")
    previous_mode = str(os.environ.get("LIVEKIT_PROVIDER") or "auto")
    os.environ["LIVEKIT_PROVIDER"] = normalized
    try:
        provider = select_livekit_provider(mode=normalized, validate_cloud=normalized == "cloud")
        apply_provider_to_env(provider)
        LIVEKIT_SELECTED_PROVIDER = provider
        # Provedor novo = credenciais novas: a falha do anterior (mesmo número de slot) não pode barrar este.
        _clear_livekit_failed_slots()
        LIVEKIT_MANAGER = LiveKitKeyManager(
            total_slots=4,
            state_file=LOGS_DIR / "livekit_state.json",
        )
        update_api_identity_snapshot(provider=provider)
        logger.info(
            "ECONOMY_TRANSPORT_PROVIDER_APPLIED target=%s provider=%s previous_mode=%s",
            normalized,
            provider.name,
            previous_mode,
        )
        return provider
    except Exception:
        os.environ["LIVEKIT_PROVIDER"] = previous_mode
        raise


def _wait_for_runtime_condition(predicate, *, timeout_seconds: float, interval_seconds: float = 0.5) -> dict:
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    snapshot = _runtime_state_snapshot()
    while time.monotonic() < deadline:
        snapshot = _runtime_state_snapshot()
        try:
            if predicate(snapshot):
                return snapshot
        except Exception:
            pass
        time.sleep(max(float(interval_seconds), 0.1))
    return snapshot


def _economy_transport_blockers_snapshot() -> dict[str, bool]:
    blockers: dict[str, bool] = {}
    try:
        from services.local_command_guard import is_pc_operator_active

        blockers["pc_operator_running"] = bool(is_pc_operator_active())
    except Exception:
        blockers["pc_operator_running"] = False
    try:
        from services.whatsapp_runtime_service import get_whatsapp_status

        wa_state = get_whatsapp_status()
        blockers["pending_confirmation"] = bool((wa_state or {}).get("pending_confirmation"))
        blockers["whatsapp_sending"] = str((wa_state or {}).get("status") or "").lower() in {"sending", "busy"}
    except Exception:
        blockers.setdefault("pending_confirmation", False)
        blockers.setdefault("whatsapp_sending", False)
    # Celular com voz na sala: a voz dele só funciona com LiveKit na nuvem (wss). Trocar para o local derrubaria o celular.
    hub = _PHONE_VOICE_STATE.get("hub")
    phone_on = bool(hub is not None and getattr(hub, "phone_connected", False))
    if not phone_on:
        _PHONE_VOICE_STATE["joined_at"] = 0.0
    blockers["remote_client_connected"] = phone_on and float(_PHONE_VOICE_STATE.get("joined_at") or 0.0) > 0.0
    return blockers


_PHONE_VOICE_STATE: dict[str, object] = {"joined_at": 0.0, "hub": None}


def _set_economy_pending_ui_reconnect(
    *,
    connection: dict[str, object],
    target_provider: str,
    reason: str,
    source: str,
    switch_id: str = "",
) -> dict[str, object]:
    room_name = str(connection.get("roomName") or connection.get("room_name") or "").strip()
    payload = {
        "id": f"economy_reconnect_{int(time.time() * 1000)}_{random.randint(1000, 9999)}",
        "created_at": time.time(),
        "switch_id": str(switch_id or "").strip(),
        "reason": str(reason or "transport_switch").strip() or "transport_switch",
        "source": str(source or "manual_endpoint").strip() or "manual_endpoint",
        "target_provider": str(target_provider or "").strip().lower(),
        "roomName": room_name,
        "connection": dict(connection),
        "requires_ui_reconnect": True,
    }
    global _economy_pending_ui_reconnect
    with _economy_pending_ui_reconnect_lock:
        _economy_pending_ui_reconnect = payload
    logger.info(
        "ECONOMY_UI_RECONNECT_PAYLOAD_CREATED id=%s provider=%s room=%s source=%s",
        payload["id"],
        payload["target_provider"],
        room_name,
        payload["source"],
    )
    logger.info("ECONOMY_UI_RECONNECT_REQUIRED id=%s room=%s", payload["id"], room_name)
    return dict(payload)


def _get_economy_pending_ui_reconnect() -> dict[str, object] | None:
    with _economy_pending_ui_reconnect_lock:
        if not _economy_pending_ui_reconnect:
            return None
        return dict(_economy_pending_ui_reconnect)


def _consume_economy_pending_ui_reconnect(reconnect_id: str) -> bool:
    global _economy_pending_ui_reconnect
    normalized = str(reconnect_id or "").strip()
    consumed_payload: dict[str, object] | None = None
    with _economy_pending_ui_reconnect_lock:
        current = _economy_pending_ui_reconnect or {}
        if normalized and str(current.get("id") or "") == normalized:
            consumed_payload = dict(current)
            _economy_pending_ui_reconnect = None
    if consumed_payload:
        logger.info(
            "ECONOMY_UI_RECONNECT_CONSUMED id=%s switch_id=%s provider=%s room=%s",
            normalized,
            consumed_payload.get("switch_id") or "",
            consumed_payload.get("target_provider") or "",
            consumed_payload.get("roomName") or "",
        )
        _note_economy_ui_reconnect_consumed(consumed_payload)
        return True
    return False


def _get_active_transport_switch() -> dict[str, object] | None:
    with _economy_active_transport_switch_lock:
        if not _economy_active_transport_switch:
            return None
        return dict(_economy_active_transport_switch)


def _set_active_transport_switch(switch_state: dict[str, object]) -> dict[str, object]:
    global _economy_active_transport_switch
    with _economy_active_transport_switch_lock:
        _economy_active_transport_switch = dict(switch_state)
        return dict(_economy_active_transport_switch)


def _update_active_transport_switch(switch_id: str, **updates) -> dict[str, object] | None:
    global _economy_active_transport_switch
    normalized = str(switch_id or "").strip()
    with _economy_active_transport_switch_lock:
        if not _economy_active_transport_switch or str(_economy_active_transport_switch.get("switch_id") or "") != normalized:
            return None
        _economy_active_transport_switch.update(updates)
        return dict(_economy_active_transport_switch)


def _clear_active_transport_switch(switch_id: str, *, reason: str) -> None:
    global _economy_active_transport_switch
    normalized = str(switch_id or "").strip()
    with _economy_active_transport_switch_lock:
        if _economy_active_transport_switch and str(_economy_active_transport_switch.get("switch_id") or "") == normalized:
            _economy_active_transport_switch = None
    logger.info("ECONOMY_TRANSPORT_TRANSITION_CLEARED switch_id=%s reason=%s", normalized, reason)


def _mark_transport_switch_finished(result: TransportSwitchResult, *, clear_reason: str) -> TransportSwitchResult:
    manager = get_economy_mode_manager()
    logger.info(
        "ECONOMY_TRANSPORT_TRANSITION_FINALIZE_BEGIN switch_id=%s status=%s provider=%s reason=%s",
        result.switch_id,
        result.status,
        result.provider,
        clear_reason,
    )
    _economy_transport_switch_manager.finish(result)
    manager.set_transport_switch_in_progress(False)
    if result.provider:
        manager.set_current_transport(result.provider)
    _clear_active_transport_switch(result.switch_id, reason=clear_reason)
    logger.info(
        "ECONOMY_TRANSPORT_TRANSITION_FINALIZE_OK switch_id=%s status=%s provider=%s",
        result.switch_id,
        result.status,
        result.provider,
    )
    return result


def _note_economy_ui_reconnect_consumed(payload: dict[str, object]) -> None:
    switch_id = str(payload.get("switch_id") or "").strip()
    if not switch_id:
        return
    reason = str(payload.get("reason") or "")
    if reason == "transport_switch_rollback":
        _update_active_transport_switch(
            switch_id,
            phase="rollback_runtime_wait",
            rollback_ui_reconnect_consumed=True,
            rollback_reconnect_consumed_at=time.time(),
        )
    else:
        _update_active_transport_switch(
            switch_id,
            phase="awaiting_runtime_ready",
            ui_reconnect_consumed=True,
            reconnect_consumed_at=time.time(),
        )
    _start_economy_transport_runtime_monitor(switch_id)


def _start_economy_transport_runtime_monitor(switch_id: str) -> None:
    normalized = str(switch_id or "").strip()
    if not normalized:
        return
    with _economy_active_transport_switch_lock:
        if normalized in _economy_transport_monitor_threads:
            return
        _economy_transport_monitor_threads.add(normalized)

    def _runner() -> None:
        try:
            asyncio.run(_monitor_economy_transport_runtime(normalized))
        finally:
            with _economy_active_transport_switch_lock:
                _economy_transport_monitor_threads.discard(normalized)

    threading.Thread(target=_runner, name=f"economy-switch-monitor-{normalized[-8:]}", daemon=True).start()


def _transition_is_expired(switch_state: dict[str, object], *, now: float | None = None) -> bool:
    current_time = time.time() if now is None else float(now)
    started = float(switch_state.get("started_at") or current_time)
    timeout = float(switch_state.get("timeout_seconds") or 90.0)
    return transition_is_stale(started_at=started, now=current_time, timeout_seconds=timeout, phase=str(switch_state.get("phase") or ""))


def _clear_stale_transport_transition_if_needed(runtime_snapshot: dict | None = None) -> bool:
    state = _get_active_transport_switch()
    if not state:
        return False
    runtime = runtime_snapshot or _runtime_state_snapshot()
    failed = bool(state.get("failed")) or bool(late_transport_failure_reason(runtime))
    stale = _transition_is_expired(state)
    if not failed and not stale:
        return False
    reason = "failed" if failed else "stale"
    logger.warning("ECONOMY_TRANSPORT_TRANSITION_STALE switch_id=%s reason=%s", state.get("switch_id") or "", reason)
    _economy_transport_switch_manager.clear_transition(reason=reason)
    get_economy_mode_manager().set_transport_switch_in_progress(False)
    _clear_active_transport_switch(str(state.get("switch_id") or ""), reason=reason)
    return True


def _reconcile_economy_transport_transition(runtime_snapshot: dict | None = None) -> dict[str, object]:
    runtime = runtime_snapshot or _runtime_state_snapshot()
    state = _get_active_transport_switch()
    manager = get_economy_mode_manager()
    target_provider = str((state or {}).get("target_provider") or provider_kind_from_snapshot(runtime)).strip().lower()
    switch_id = str((state or {}).get("switch_id") or "").strip()
    if state and target_provider in {"cloud", "local"} and runtime_ready_for_transport(runtime, target_provider):
        logger.info(
            "ECONOMY_TRANSPORT_STATUS_RECONCILED switch_id=%s status=functional_ready provider=%s phase=%s",
            switch_id,
            target_provider,
            state.get("phase") or "",
        )
        manager.clear_last_switch_error()
        manager.reset_idle_timer(reason=f"transport_switch_reconciled_{target_provider}")
        _mark_transport_switch_finished(
            TransportSwitchResult(
                ok=True,
                status="functional_ready",
                from_provider=str(state.get("from_provider") or ""),
                to_provider=target_provider,
                provider=target_provider,
                reason="runtime_functional_ready_reconciled",
                switch_id=switch_id,
            ),
            clear_reason="status_reconciled",
        )
        return {"active": False, "status": "functional_ready", "provider": target_provider, "switch_id": switch_id}
    if state:
        phase = str(state.get("phase") or "unknown")
        if _clear_stale_transport_transition_if_needed(runtime):
            logger.info(
                "ECONOMY_TRANSPORT_STATUS_RECONCILED switch_id=%s status=cleared reason=stale_or_failed",
                switch_id,
            )
            return {"active": False, "status": "cleared", "provider": provider_kind_from_snapshot(runtime), "switch_id": switch_id}
        logger.info(
            "ECONOMY_TRANSPORT_TRANSITION_STILL_ACTIVE switch_id=%s reason=%s provider=%s",
            switch_id,
            phase,
            target_provider,
        )
        return {"active": True, "status": phase, "provider": target_provider, "switch_id": switch_id}
    if (
        _economy_transport_switch_manager.switch_in_progress
        and not _economy_transport_switch_lock.locked()
        and runtime_ready_for_transport(runtime, provider_kind_from_snapshot(runtime))
    ):
        provider = provider_kind_from_snapshot(runtime)
        _economy_transport_switch_manager.clear_transition(reason="status_reconciled_no_active_state")
        manager.set_transport_switch_in_progress(False)
        manager.set_current_transport(provider)
        logger.info(
            "ECONOMY_TRANSPORT_STATUS_RECONCILED switch_id= status=functional_ready provider=%s reason=no_active_state",
            provider,
        )
        return {"active": False, "status": "functional_ready", "provider": provider, "switch_id": ""}
    return {
        "active": bool(state or _economy_transport_switch_manager.switch_in_progress),
        "status": "idle",
        "provider": provider_kind_from_snapshot(runtime),
        "switch_id": switch_id,
    }


async def _monitor_economy_transport_runtime(switch_id: str) -> None:
    state = _get_active_transport_switch()
    if not state:
        return
    if str(state.get("switch_id") or "") != str(switch_id or ""):
        return
    target_provider = str(state.get("target_provider") or "").strip().lower()
    timeout_seconds = max(float(state.get("timeout_seconds") or 90.0), 10.0)
    deadline = time.monotonic() + timeout_seconds
    logger.info(
        "ECONOMY_TRANSPORT_SWITCH_RUNTIME_WAIT_BEGIN switch_id=%s provider=%s timeout_seconds=%s",
        switch_id,
        target_provider,
        int(timeout_seconds),
    )
    last_failure = ""
    while time.monotonic() < deadline:
        snapshot = _runtime_state_snapshot()
        if runtime_ready_for_transport(snapshot, target_provider):
            logger.info(
                "ECONOMY_TRANSPORT_SWITCH_RUNTIME_READY switch_id=%s provider=%s room=%s",
                switch_id,
                target_provider,
                snapshot.get("room_name") or "",
            )
            logger.info(
                "ECONOMY_TRANSPORT_SWITCH_OK from=%s to=%s provider=%s room=%s",
                state.get("from_provider") or "",
                target_provider,
                target_provider,
                snapshot.get("room_name") or "",
            )
            manager = get_economy_mode_manager()
            manager.set_current_transport(target_provider)
            manager.reset_idle_timer(reason=f"transport_switch_ok_{target_provider}")
            manager.clear_last_switch_error()
            _mark_transport_switch_finished(
                TransportSwitchResult(
                    ok=True,
                    status="functional_ready",
                    from_provider=str(state.get("from_provider") or ""),
                    to_provider=target_provider,
                    provider=target_provider,
                    reason="runtime_functional_ready",
                    switch_id=switch_id,
                ),
                clear_reason="runtime_ready",
            )
            return
        failure_reason = late_transport_failure_reason(snapshot)
        if failure_reason:
            last_failure = failure_reason
            break
        await asyncio.sleep(0.75)
    if not last_failure:
        last_failure = "runtime_ready_timeout"
    logger.warning(
        "ECONOMY_TRANSPORT_SWITCH_LATE_FAILURE_DETECTED switch_id=%s provider=%s reason=%s",
        switch_id,
        target_provider,
        last_failure,
    )
    await _handle_transport_switch_failure(state, reason=last_failure, late_failure=True)


async def _handle_transport_switch_failure(
    switch_state: dict[str, object],
    *,
    reason: str,
    late_failure: bool = False,
) -> dict[str, object]:
    switch_id = str(switch_state.get("switch_id") or "").strip()
    normalized_target = str(switch_state.get("target_provider") or "").strip().lower()
    previous_kind = str(switch_state.get("previous_provider") or "").strip().lower()
    current_provider = str(switch_state.get("from_provider") or provider_kind_from_snapshot(_runtime_state_snapshot()))
    config = get_economy_mode_manager().get_config()
    timeout_seconds = max(float(switch_state.get("timeout_seconds") or config.get("handoff_timeout_seconds", 90)), 10.0)
    rollback_enabled = bool(switch_state.get("rollback_enabled", config.get("rollback_on_handoff_failure", True)))
    logger.error("ECONOMY_TRANSPORT_SWITCH_FAILED reason=%s switch_id=%s late=%s", reason, switch_id, str(bool(late_failure)).lower())
    _update_active_transport_switch(switch_id, phase="failed", failed=True, failure_reason=reason)
    rollback_attempted = False
    rollback_ok = False
    rollback_provider = provider_kind_from_snapshot(_runtime_state_snapshot())
    if rollback_enabled and previous_kind in {"cloud", "local"}:
        rollback_attempted = True
        rollback_ok = await _rollback_economy_transport_switch(
            switch_state,
            previous_kind=previous_kind,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
        rollback_provider = previous_kind if rollback_ok else provider_kind_from_snapshot(_runtime_state_snapshot())
    cooldown = _economy_transport_switch_manager.set_cooldown(int(config.get("handoff_cooldown_seconds", 300)))
    manager = get_economy_mode_manager()
    manager.set_handoff_cooldown(cooldown)
    manager.set_last_switch_error(reason)
    result = TransportSwitchResult(
        ok=False,
        status="failed",
        from_provider=current_provider,
        to_provider=normalized_target,
        provider=rollback_provider,
        reason="switch_failed",
        error=reason,
        rollback_attempted=rollback_attempted,
        rollback_ok=rollback_ok,
        cooldown_seconds=cooldown,
        next_handoff_allowed_after_seconds=cooldown,
        next_handoff_allowed_at=_economy_transport_switch_manager.next_allowed_at,
        switch_id=switch_id,
    )
    _mark_transport_switch_finished(result, clear_reason="failed")
    logger.info("ECONOMY_TRANSPORT_SWITCH_SESSION_TRANSITION_CLEAR reason=%s switch_id=%s", reason, switch_id)
    return {
        "ok": False,
        "status": "failed",
        "from_provider": current_provider,
        "to_provider": normalized_target,
        "provider": rollback_provider,
        "error": reason,
        "rollback_attempted": rollback_attempted,
        "rollback_ok": rollback_ok,
        "next_handoff_allowed_after_seconds": cooldown,
    }


async def _rollback_economy_transport_switch(
    switch_state: dict[str, object],
    *,
    previous_kind: str,
    reason: str,
    timeout_seconds: float,
) -> bool:
    switch_id = str(switch_state.get("switch_id") or "").strip()
    source = str(switch_state.get("source") or "transport_switch").strip() or "transport_switch"
    logger.info("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_BEGIN switch_id=%s from=%s to=%s reason=%s", switch_id, switch_state.get("target_provider") or "", previous_kind, reason)
    _update_active_transport_switch(switch_id, phase="rollback_begin")
    try:
        await asyncio.to_thread(_apply_livekit_provider_for_transport_switch, previous_kind)
        logger.info("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_PROVIDER_RESTORED provider=%s", previous_kind)
        if previous_kind == "local" and not await asyncio.to_thread(start_livekit_server):
            raise RuntimeError("rollback_local_livekit_unavailable")
        logger.info("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_AGENT_START_BEGIN provider=%s", previous_kind)
        await asyncio.to_thread(_restart_agent_for_transport_switch, timeout_seconds=timeout_seconds)
        logger.info("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_AGENT_READY provider=%s", previous_kind)
        rollback_connection = await _prepare_transport_switch_session_payload(
            target_kind=previous_kind,
            timeout_seconds=timeout_seconds,
            participant_name=str(_session_debug_snapshot().get("participant_name") or ""),
        )
        pending = _set_economy_pending_ui_reconnect(
            connection=dict(rollback_connection),
            target_provider=previous_kind,
            reason="transport_switch_rollback",
            source=source,
            switch_id=switch_id,
        )
        logger.info(
            "ECONOMY_TRANSPORT_SWITCH_ROLLBACK_UI_RECONNECT_REQUIRED switch_id=%s id=%s room=%s",
            switch_id,
            pending.get("id") or "",
            rollback_connection.get("roomName") or rollback_connection.get("room_name"),
        )
        _update_active_transport_switch(
            switch_id,
            phase="rollback_awaiting_ui_reconnect",
            rollback_reconnect_id=pending.get("id") or "",
            rollback_target_provider=previous_kind,
        )
        reconnect_deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < reconnect_deadline:
            current = _get_active_transport_switch() or {}
            if bool(current.get("rollback_ui_reconnect_consumed")):
                break
            await asyncio.sleep(0.5)
        runtime_deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < runtime_deadline:
            snapshot = _runtime_state_snapshot()
            if runtime_ready_for_transport(snapshot, previous_kind):
                logger.info(
                    "ECONOMY_TRANSPORT_SWITCH_ROLLBACK_RUNTIME_READY switch_id=%s provider=%s room=%s",
                    switch_id,
                    previous_kind,
                    snapshot.get("room_name") or "",
                )
                logger.info("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_OK switch_id=%s provider=%s", switch_id, previous_kind)
                get_economy_mode_manager().set_current_transport(previous_kind)
                return True
            rollback_failure = late_transport_failure_reason(snapshot)
            if rollback_failure:
                raise RuntimeError(f"rollback_runtime_failed:{rollback_failure}")
            await asyncio.sleep(0.75)
        raise RuntimeError("rollback_runtime_ready_timeout")
    except Exception as exc:
        logger.exception("ECONOMY_TRANSPORT_SWITCH_ROLLBACK_FAILED reason=%s", exc)
        return False


async def _execute_economy_transport_switch(target_kind: str, *, source: str = "manual_endpoint") -> dict[str, object]:
    normalized_target = str(target_kind or "").strip().lower()
    if normalized_target not in {"local", "cloud"}:
        return {"ok": False, "status": "failed", "error": f"unsupported_target:{target_kind}"}

    manager = get_economy_mode_manager()
    config = manager.get_config()
    runtime_snapshot = _runtime_state_snapshot()
    _reconcile_economy_transport_transition(runtime_snapshot)
    runtime_snapshot = _runtime_state_snapshot()
    blockers = _economy_transport_blockers_snapshot()
    current_provider = provider_kind_from_snapshot(runtime_snapshot)
    logger.info(
        "ECONOMY_TRANSPORT_SWITCH_REQUESTED from=%s to=%s source=%s",
        current_provider,
        normalized_target,
        source,
    )

    recovery_to_cloud = False
    active_switch = _get_active_transport_switch()
    if normalized_target == "cloud" and recovery_to_cloud_allowed(
        runtime_snapshot,
        switch_in_progress=_economy_transport_switch_manager.switch_in_progress,
        switch_failed=bool((active_switch or {}).get("failed")),
        switch_stale=_transition_is_expired(active_switch) if active_switch else False,
    ):
        recovery_to_cloud = True
        logger.info("ECONOMY_TRANSPORT_RECOVERY_TO_CLOUD_ALLOWED provider=%s reason=local_degraded", current_provider)
        if active_switch:
            _economy_transport_switch_manager.clear_transition(reason="recovery_to_cloud")
            manager.set_transport_switch_in_progress(False)
            _clear_active_transport_switch(str(active_switch.get("switch_id") or ""), reason="recovery_to_cloud")

    if normalized_target == "local":
        decision = _economy_transport_switch_manager.can_start_manual_switch(config, runtime_snapshot, blockers)
    else:
        decision = (
            TransportDecision(True, "manual_recovery_allowed", current_provider, to_provider="cloud")
            if recovery_to_cloud
            else _economy_transport_switch_manager.can_start_manual_cloud_switch(config, runtime_snapshot, blockers)
        )
    if not decision.should_switch:
        if normalized_target == "cloud" and current_provider == "local":
            logger.info("ECONOMY_TRANSPORT_RECOVERY_TO_CLOUD_BLOCKED reason=%s", decision.reason)
        if str(decision.reason or "").startswith("cooldown:"):
            logger.info("ECONOMY_TRANSPORT_SWITCH_BLOCKED reason=cooldown remaining=%s", _economy_transport_switch_manager.cooldown_remaining())
        else:
            logger.info("ECONOMY_TRANSPORT_SWITCH_BLOCKED reason=%s", decision.reason)
        return {
            "ok": False,
            "status": "blocked",
            "reason": decision.reason,
            "from_provider": decision.from_provider,
            "to_provider": normalized_target,
            "provider": current_provider,
            "blocked_reasons": transport_block_reasons(config=config, runtime_snapshot=runtime_snapshot, explicit=blockers),
            "cooldown_remaining_seconds": _economy_transport_switch_manager.cooldown_remaining(),
        }

    if not _economy_transport_switch_lock.acquire(blocking=False):
        logger.info("ECONOMY_TRANSPORT_SWITCH_BLOCKED reason=switch_lock_busy")
        return {
            "ok": False,
            "status": "blocked",
            "reason": "switch_lock_busy",
            "from_provider": current_provider,
            "to_provider": normalized_target,
            "provider": current_provider,
        }

    previous_provider = _selected_livekit_provider()
    previous_kind = provider_kind_from_snapshot(
        {
            "livekit_provider_type": getattr(previous_provider, "kind", current_provider),
            "livekit_url": getattr(previous_provider, "url", ""),
        }
    )
    timeout_seconds = max(float(config.get("handoff_timeout_seconds", 60)), 10.0)
    rollback_enabled = bool(config.get("rollback_on_handoff_failure", True))
    _economy_transport_switch_manager.begin()
    manager.set_transport_switch_in_progress(True)
    pending_handoff_started = False
    switch_id = f"transport_switch_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    try:
        logger.info("ECONOMY_TRANSPORT_SWITCH_BEGIN from=%s to=%s", current_provider, normalized_target)
        target_provider = await asyncio.to_thread(_apply_livekit_provider_for_transport_switch, normalized_target)
        if normalized_target == "local":
            if not await asyncio.to_thread(start_livekit_server):
                raise RuntimeError("local_livekit_unavailable")
            logger.info("ECONOMY_TRANSPORT_SWITCH_LOCAL_READY url=%s", target_provider.url)

        if normalized_target == "local":
            logger.info("AGENT_LOCAL_START_BEGIN switch_id=%s", switch_id)
            logger.info(
                "AGENT_LOCAL_ENV python=%s venv=%s",
                BOOT_PYTHON_EXE,
                os.getenv("KIRA_VENV_LABEL") or "",
            )
        await asyncio.to_thread(_restart_agent_for_transport_switch, timeout_seconds=timeout_seconds)
        if normalized_target == "local":
            logger.info("AGENT_LOCAL_IMPORTS_OK switch_id=%s", switch_id)
        connection = await _prepare_transport_switch_session_payload(
            target_kind=normalized_target,
            timeout_seconds=timeout_seconds,
            participant_name=str(_session_debug_snapshot().get("participant_name") or ""),
        )
        if normalized_target == "local":
            logger.info("AGENT_LOCAL_ROOM_CONNECT_BEGIN room=%s", connection.get("roomName") or connection.get("room_name"))
        logger.info("ECONOMY_TRANSPORT_SWITCH_ROOM_PREPARED room=%s", connection.get("roomName") or connection.get("room_name"))
        logger.info("ECONOMY_TRANSPORT_SWITCH_UI_RECONNECT_BEGIN room=%s", connection.get("roomName") or connection.get("room_name"))
        manager.set_current_transport(normalized_target)
        pending_handoff_started = True
        _set_active_transport_switch(
            {
                "switch_id": switch_id,
                "started_at": time.time(),
                "phase": "awaiting_ui_reconnect",
                "from_provider": current_provider,
                "target_provider": normalized_target,
                "previous_provider": previous_kind,
                "source": source,
                "timeout_seconds": timeout_seconds,
                "rollback_enabled": rollback_enabled,
                "connection": dict(connection),
            }
        )
        result = TransportSwitchResult(
            ok=False,
            status="awaiting_ui_reconnect",
            from_provider=current_provider,
            to_provider=normalized_target,
            provider=normalized_target,
            reason="ui_reconnect_required",
            connection=dict(connection),
            switch_id=switch_id,
        )
        pending_reconnect = _set_economy_pending_ui_reconnect(
            connection=dict(connection),
            target_provider=normalized_target,
            reason="transport_switch_pending",
            source=source,
            switch_id=switch_id,
        )
        _start_economy_transport_runtime_monitor(switch_id)
        return {
            "ok": False,
            "status": result.status,
            "state": "awaiting_ui_reconnect",
            "switch_id": switch_id,
            "from_provider": result.from_provider,
            "to_provider": result.to_provider,
            "provider_target": result.to_provider,
            "provider": result.provider,
            "reason": result.reason,
            "connection": result.connection,
            "requires_ui_reconnect": True,
            "ui_reconnect": pending_reconnect,
            "status_after": manager.get_status(runtime_snapshot=_runtime_state_snapshot()),
        }
    except Exception as exc:
        error_text = str(exc)
        logger.exception("ECONOMY_TRANSPORT_SWITCH_FAILED reason=%s", error_text)
        failure_state = {
            "switch_id": switch_id,
            "started_at": time.time(),
            "phase": "failed",
            "from_provider": current_provider,
            "target_provider": normalized_target,
            "previous_provider": previous_kind,
            "source": source,
            "timeout_seconds": timeout_seconds,
            "rollback_enabled": rollback_enabled,
        }
        _set_active_transport_switch(failure_state)
        return await _handle_transport_switch_failure(failure_state, reason=error_text, late_failure=False)
    finally:
        if not pending_handoff_started:
            manager.set_transport_switch_in_progress(False)
        if _economy_transport_switch_manager.switch_in_progress and not pending_handoff_started:
            _economy_transport_switch_manager.finish(
                TransportSwitchResult(
                    ok=False,
                    status="interrupted",
                    from_provider=current_provider,
                    to_provider=normalized_target,
                    provider=provider_kind_from_snapshot(_runtime_state_snapshot()),
                    reason="finally_cleanup",
                )
            )
        _economy_transport_switch_lock.release()


def _restart_agent_for_transport_switch(*, timeout_seconds: float) -> dict:
    global _agent_proc
    logger.info("ECONOMY_TRANSPORT_SWITCH_AGENT_RECONNECT_BEGIN")
    switch_started_at = time.time()
    before_snapshot = _runtime_state_snapshot()
    watched_proc = _agent_proc
    watched_pid = int(getattr(watched_proc, "pid", 0) or 0) if watched_proc is not None else 0
    _log_transport_precheck_processes(expected_agent_pid=watched_pid)
    logger.info(
        "ECONOMY_TRANSPORT_AGENT_OLD_IDENTIFIED pid=%s provider=%s room=%s worker_ready=%s",
        watched_pid or "none",
        provider_kind_from_snapshot(before_snapshot),
        before_snapshot.get("room_name") or before_snapshot.get("requested_room_name") or "",
        str(bool(before_snapshot.get("agent_worker_ready"))).lower(),
    )
    logger.info("ECONOMY_TRANSPORT_AGENT_PID_OLD pid=%s", watched_pid or "none")
    if watched_proc is not None:
        logger.info("ECONOMY_TRANSPORT_AGENT_STOP_BEGIN pid=%s", watched_pid)
        _terminate_agent_process_instance(watched_proc, expected=True)
        stopped = _wait_for_agent_process_exit(watched_proc, timeout_seconds=min(max(timeout_seconds / 3.0, 5.0), 20.0))
        if stopped:
            logger.info("ECONOMY_TRANSPORT_AGENT_OLD_EXIT_EXPECTED pid=%s", watched_pid)
        _remove_agent_locks_after_expected_exit(
            expected_pid=watched_pid,
            expected_process_terminated=stopped,
            switch_started_at=switch_started_at,
        )
        _wait_for_worker_port_released(timeout_seconds=min(max(timeout_seconds / 3.0, 5.0), 20.0))
        logger.info("ECONOMY_TRANSPORT_AGENT_STOP_OK pid=%s stopped=%s", watched_pid, str(stopped).lower())
    else:
        _remove_agent_locks_after_expected_exit(
            expected_pid=watched_pid,
            expected_process_terminated=True,
            switch_started_at=switch_started_at,
        )
    _cleanup_duplicate_agent_processes_for_transport_switch(expected_pid=0, reason="before_agent_restart")
    if not _wait_for_worker_port_released(timeout_seconds=min(max(timeout_seconds / 3.0, 5.0), 20.0)):
        logger.error("ECONOMY_TRANSPORT_AGENT_DUPLICATE_PREVENTED reason=port_8081_still_occupied")
        raise RuntimeError("agent_duplicate_prevented:port_8081_still_occupied")
    _agent_ready.clear()
    _update_runtime_readiness(
        agent_process_spawned=False,
        agent_process_dead=False,
        agent_connected=False,
        agent_ready=False,
        agent_worker_ready=False,
        room_connected=False,
        room_ready=False,
        runtime_ready=False,
        voice_pipeline_ready=False,
        functional_ready=False,
        ui_status="connecting",
        event="ECONOMY_TRANSPORT_SWITCH_AGENT_RECONNECT_BEGIN",
        message="Reiniciando agente para trocar provider LiveKit.",
        error="",
        failure_reason="",
    )
    logger.info("ECONOMY_TRANSPORT_AGENT_START_BEGIN provider=%s", provider_kind_from_snapshot(_runtime_state_snapshot()))
    start_agent()
    new_pid = int(getattr(_agent_proc, "pid", 0) or 0) if _agent_proc is not None else 0
    if not new_pid:
        logger.error("ECONOMY_TRANSPORT_AGENT_READY_FAILED reason=agent_process_not_spawned")
        raise RuntimeError("agent_process_not_spawned_after_transport_switch")
    logger.info("ECONOMY_TRANSPORT_AGENT_PID_NEW pid=%s", new_pid)
    logger.info("ECONOMY_TRANSPORT_AGENT_START_OK pid=%s", new_pid)
    _cleanup_duplicate_agent_processes_for_transport_switch(expected_pid=new_pid, reason="after_agent_restart")
    logger.info("ECONOMY_TRANSPORT_AGENT_READY_WAIT_BEGIN pid=%s timeout_seconds=%s", new_pid, int(timeout_seconds))
    snapshot = _wait_for_runtime_condition(
        lambda state: (
            bool(state.get("agent_worker_ready"))
            and not bool(state.get("agent_process_dead"))
            and _agent_proc is not None
            and getattr(_agent_proc, "poll", lambda: None)() is None
        ),
        timeout_seconds=timeout_seconds,
    )
    if not bool(snapshot.get("agent_worker_ready")):
        _log_transport_agent_ready_timeout_context(
            pid=new_pid,
            timeout_seconds=timeout_seconds,
            snapshot=snapshot,
            phase="primary_timeout",
        )
        process_alive = False
        try:
            process_alive = _agent_proc is not None and int(getattr(_agent_proc, "pid", 0) or 0) == new_pid and _agent_proc.poll() is None
        except Exception:
            process_alive = False
        if process_alive:
            grace_seconds = min(max(float(timeout_seconds) / 2.0, 30.0), 60.0)
            logger.info("ECONOMY_TRANSPORT_AGENT_READY_GRACE_BEGIN pid=%s timeout_seconds=%s", new_pid, int(grace_seconds))
            snapshot = _wait_for_runtime_condition(
                lambda state: (
                    bool(state.get("agent_worker_ready"))
                    and not bool(state.get("agent_process_dead"))
                    and _agent_proc is not None
                    and int(getattr(_agent_proc, "pid", 0) or 0) == new_pid
                    and getattr(_agent_proc, "poll", lambda: None)() is None
                ),
                timeout_seconds=grace_seconds,
            )
            if bool(snapshot.get("agent_worker_ready")):
                logger.info("ECONOMY_TRANSPORT_AGENT_READY_GRACE_OK pid=%s", new_pid)
                logger.info("ECONOMY_TRANSPORT_AGENT_READY_OK pid=%s", new_pid)
                return snapshot
            _log_transport_agent_ready_timeout_context(
                pid=new_pid,
                timeout_seconds=grace_seconds,
                snapshot=snapshot,
                phase="grace_timeout",
            )
            logger.error("ECONOMY_TRANSPORT_AGENT_READY_GRACE_FAILED pid=%s", new_pid)
        reason = str(snapshot.get("agent_last_error") or snapshot.get("failure_reason") or snapshot.get("error") or "agent_worker_not_ready_after_transport_switch")
        logger.error("ECONOMY_TRANSPORT_AGENT_READY_FAILED reason=%s", reason)
        raise RuntimeError("agent_worker_not_ready_after_transport_switch")
    logger.info("ECONOMY_TRANSPORT_AGENT_READY_OK pid=%s", new_pid)
    return snapshot


def _wait_for_agent_process_exit(proc, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while time.monotonic() < deadline:
        try:
            if proc is None or proc.poll() is not None:
                return True
        except Exception:
            return True
        time.sleep(0.25)
    return False


def _wait_for_worker_port_released(*, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                if sock.connect_ex(("127.0.0.1", 8081)) != 0:
                    return True
        except Exception:
            return True
        time.sleep(0.25)
    logger.warning("ECONOMY_TRANSPORT_AGENT_STOP_OK port_8081_released=false")
    return False


def _transport_port_open(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False


def _log_transport_agent_ready_timeout_context(*, pid: int, timeout_seconds: float, snapshot: dict, phase: str) -> None:
    proc = _agent_proc
    process_alive = False
    exit_code = "unknown"
    try:
        if proc is not None and int(getattr(proc, "pid", 0) or 0) == int(pid):
            polled = proc.poll()
            process_alive = polled is None
            exit_code = "none" if process_alive else str(polled)
    except Exception as exc:
        exit_code = f"poll_error:{exc.__class__.__name__}"
    worker_ready, worker_detail = _probe_livekit_worker_http_ready(LIVEKIT_WORKER_HTTP_URL, expected_proc=proc)
    entrypoint_lock = BASE_DIR / "runtime" / "entrypoint.lock"
    bootstrap_lock = _bootstrap_lock_path_for_transport_switch()
    logger.error(
        "ECONOMY_TRANSPORT_AGENT_READY_TIMEOUT_CONTEXT phase=%s pid=%s timeout_seconds=%s process_alive=%s exit_code=%s "
        "port_8081_open=%s worker_http_ready=%s worker_http_detail=%s ready_event_set=%s "
        "agent_worker_ready=%s agent_process_dead=%s provider=%s runtime_event=%s runtime_error=%s failure_reason=%s",
        phase,
        pid,
        int(timeout_seconds),
        str(bool(process_alive)).lower(),
        exit_code,
        str(_transport_port_open(8081)).lower(),
        str(bool(worker_ready)).lower(),
        worker_detail,
        str(_agent_ready.is_set()).lower(),
        str(bool((snapshot or {}).get("agent_worker_ready"))).lower(),
        str(bool((snapshot or {}).get("agent_process_dead"))).lower(),
        provider_kind_from_snapshot(snapshot or {}),
        str((snapshot or {}).get("event") or ""),
        str((snapshot or {}).get("error") or (snapshot or {}).get("agent_last_error") or ""),
        str((snapshot or {}).get("failure_reason") or ""),
    )
    logger.error(
        "ECONOMY_TRANSPORT_AGENT_READY_TIMEOUT_LOCKS phase=%s entrypoint_lock_exists=%s bootstrap_lock_exists=%s entrypoint_lock=%s bootstrap_lock=%s",
        phase,
        str(entrypoint_lock.exists()).lower(),
        str(bootstrap_lock.exists()).lower(),
        entrypoint_lock,
        bootstrap_lock,
    )


def _is_kira_agent_process(proc) -> bool:
    try:
        cmdline = " ".join(proc.cmdline() or [])
        cwd = proc.cwd()
    except Exception:
        return False
    cmd_lower = cmdline.lower()
    base_lower = str(BASE_DIR).lower()
    agent_lower = str(AGENT_DIR).lower()
    return (
        "agent.py" in cmd_lower
        and "start" in cmd_lower
        and (base_lower in cmd_lower or str(cwd or "").lower().startswith(agent_lower))
    )


def _cleanup_duplicate_agent_processes_for_transport_switch(*, expected_pid: int = 0, reason: str) -> list[int]:
    logger.info("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_CHECK reason=%s expected_pid=%s", reason, expected_pid or "none")
    cleaned: list[int] = []
    try:
        import psutil
    except Exception as exc:
        logger.warning("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_SKIPPED reason=psutil_unavailable:%s", type(exc).__name__)
        return cleaned
    current_pid = os.getpid()
    expected_family: set[int] = set()
    if int(expected_pid or 0) > 0:
        try:
            expected_proc = psutil.Process(int(expected_pid))
            expected_family = {int(child.pid) for child in expected_proc.children(recursive=True)}
        except Exception:
            expected_family = set()
    for proc in psutil.process_iter(["pid", "name"]):
        pid = int(proc.info.get("pid") or 0)
        if pid <= 0 or pid in {current_pid, int(expected_pid or 0)} or pid in expected_family:
            if pid in expected_family:
                logger.info(
                    "ECONOMY_TRANSPORT_DUPLICATE_PROCESS_SKIPPED pid=%s reason=expected_agent_child expected_pid=%s",
                    pid,
                    expected_pid,
                )
            continue
        if not _is_kira_agent_process(proc):
            continue
        logger.warning("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_FOUND pid=%s name=%s reason=%s", pid, proc.info.get("name") or "", reason)
        try:
            with _livekit_failed_slots_lock:
                _agent_expected_exit_pids.add(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            cleaned.append(pid)
            logger.info("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_CLEANED pid=%s reason=%s", pid, reason)
        except Exception as exc:
            logger.warning("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_SKIPPED pid=%s reason=cleanup_failed:%s", pid, type(exc).__name__)
    if not cleaned:
        logger.info("ECONOMY_TRANSPORT_DUPLICATE_PROCESS_CHECK result=ok reason=%s", reason)
    return cleaned


def _log_transport_precheck_processes(*, expected_agent_pid: int = 0) -> None:
    logger.info("ECONOMY_TRANSPORT_PRECHECK_PROCESSES_BEGIN expected_agent_pid=%s", expected_agent_pid or "none")
    cleaned = _cleanup_duplicate_agent_processes_for_transport_switch(
        expected_pid=expected_agent_pid,
        reason="transport_precheck",
    )
    logger.info("ECONOMY_TRANSPORT_PRECHECK_PROCESSES_OK duplicate_agents_cleaned=%s", len(cleaned))
    port_states = {port: _transport_port_open(port) for port in (3000, 7880, 8081)}
    logger.info(
        "ECONOMY_TRANSPORT_PRECHECK_PORTS_OK port_3000_open=%s port_7880_open=%s port_8081_open=%s",
        str(port_states[3000]).lower(),
        str(port_states[7880]).lower(),
        str(port_states[8081]).lower(),
    )


def _bootstrap_lock_path_for_transport_switch() -> Path:
    preferred = Path("/tmp/kira_bootstrap.lock")
    if os.name != "nt" or preferred.parent.exists():
        return preferred
    return Path(tempfile.gettempdir()) / "kira_bootstrap.lock"


def _log_transport_lock_cleanup_result(result) -> None:
    logger.info(
        "ECONOMY_TRANSPORT_LOCK_CHECK path=%s exists=%s lock_pid=%s expected_pid=%s active=%s age_seconds=%.2f",
        result.path,
        str(bool(result.existed)).lower(),
        result.lock_pid,
        result.expected_pid,
        str(bool(result.lock_pid_active)).lower(),
        float(result.lock_age_seconds or 0.0),
    )
    if not result.existed:
        logger.info("ECONOMY_TRANSPORT_LOCK_REMOVE_SKIPPED reason=missing path=%s", result.path)
        return
    if result.lock_pid_active and not result.removed:
        logger.warning(
            "ECONOMY_TRANSPORT_LOCK_OWNED_BY_ACTIVE_PROCESS path=%s lock_pid=%s expected_pid=%s",
            result.path,
            result.lock_pid,
            result.expected_pid,
        )
    if result.reason in {"stale_pid", "invalid_pid", "expected_process_terminated"}:
        logger.info("ECONOMY_TRANSPORT_LOCK_STALE path=%s reason=%s lock_pid=%s", result.path, result.reason, result.lock_pid)
    if result.removed:
        logger.info("ECONOMY_TRANSPORT_LOCK_REMOVED path=%s reason=%s lock_pid=%s", result.path, result.reason, result.lock_pid)
    else:
        logger.warning("ECONOMY_TRANSPORT_LOCK_REMOVE_SKIPPED reason=%s path=%s", result.reason, result.path)


def _remove_agent_locks_after_expected_exit(
    *,
    expected_pid: int,
    expected_process_terminated: bool,
    switch_started_at: float,
) -> None:
    del switch_started_at
    try:
        import psutil

        pid_exists = psutil.pid_exists
    except Exception:
        pid_exists = lambda _pid: True
    lock_paths = [
        BASE_DIR / "runtime" / "entrypoint.lock",
        _bootstrap_lock_path_for_transport_switch(),
    ]
    for lock_path in lock_paths:
        result = cleanup_transport_lock_if_safe(
            lock_path,
            switch_in_progress=_economy_transport_switch_manager.switch_in_progress,
            expected_pid=expected_pid,
            expected_process_terminated=expected_process_terminated,
            pid_exists=pid_exists,
        )
        _log_transport_lock_cleanup_result(result)


async def _prepare_transport_switch_session_payload(
    *,
    target_kind: str,
    timeout_seconds: float,
    participant_name: str = "",
) -> dict[str, object]:
    room_name = f"kira_session_{int(time.time() * 1000)}_{target_kind}"
    participant = str(participant_name or "").strip() or f"Usuario_{random.randint(1000, 9999)}"
    _reset_session_debug_for_new_attempt(room_name=room_name, participant_name=participant)
    _update_runtime_readiness(
        requested_room_name=room_name,
        room_name="",
        room_connecting=True,
        room_connected=False,
        room_ready=False,
        gemini_connecting=False,
        gemini_ready=False,
        session_started=False,
        handlers_registered=False,
        voice_pipeline_ready=False,
        runtime_ready=False,
        ui_ready=False,
        startup_greeting_created=False,
        startup_greeting_sanitized=False,
        startup_greeting_enqueued=False,
        startup_greeting_dispatched=False,
        startup_greeting_speaking=False,
        startup_greeting_done=False,
        functional_ready=False,
        boot_recovery_active=False,
        boot_state="transport_switching",
        ui_status="connecting",
        event="ECONOMY_TRANSPORT_SWITCH_ROOM_PREPARE_BEGIN",
        message="Preparando nova room LiveKit para handoff.",
        error="",
        failure_reason="",
        boot_started_at=time.monotonic(),
    )
    session_payload = await asyncio.wait_for(
        _create_livekit_session_payload_with_timeout(
            room_name=room_name,
            participant_name=participant,
            create_dispatch=False,
        ),
        timeout=max(float(timeout_seconds), 10.0),
    )
    session_payload = _save_prepared_session(session_payload)
    credential = _resolve_livekit_credential_for_room(room_name)
    if credential is None:
        raise RuntimeError("transport_switch_credential_unavailable")
    dispatch_info = await asyncio.wait_for(
        _ensure_agent_dispatch(room_name, credential),
        timeout=max(float(SELFHOST_DISPATCH_TIMEOUT_SECONDS), 30.0),
    )
    if not isinstance(dispatch_info, dict) or not str(dispatch_info.get("id") or "").strip():
        raise RuntimeError("transport_switch_dispatch_not_created")
    session_payload["dispatchInfo"] = dict(dispatch_info)
    session_payload["agentDispatch"] = dict(dispatch_info)
    session_payload["selfhostDispatchMode"] = "explicit_dispatch"
    session_payload["transportSwitch"] = True
    session_payload["targetProvider"] = target_kind
    _update_session_debug(
        room_name=room_name,
        dispatch_info=dict(dispatch_info),
        selfhost_dispatch_mode="explicit_dispatch",
        last_event="ECONOMY_TRANSPORT_SWITCH_ROOM_PREPARED",
        last_message="Room do handoff preparada. Aguardando reconexao da UI.",
        last_error="",
        last_stage="transport_switch_room_prepared",
    )
    return session_payload


def is_livekit_running(port: int = LIVEKIT_PORT) -> bool:
    """Verifica se o LiveKit local esta aceitando conexoes."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False


def start_livekit_server() -> bool:
    """Inicia o LiveKit self-host local quando necessario."""
    global _livekit_process, _livekit_started_here

    if is_livekit_running():
        logger.info("LIVEKIT_SERVER_ALREADY_RUNNING port=%s", LIVEKIT_PORT)
        return True

    if not LIVEKIT_SERVER_PATH.exists():
        logger.error("LIVEKIT_SERVER_NOT_FOUND path=%s", LIVEKIT_SERVER_PATH)
        return False

    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        _logs_dir = BASE_DIR / "logs"
        _logs_dir.mkdir(exist_ok=True)
        _livekit_log_path = _logs_dir / "livekit_server.log"
        try:
            _livekit_log_file = open(_livekit_log_path, "w", encoding="utf-8")
            logger.info("LIVEKIT_SERVER_LOG_ENABLED path=%s", _livekit_log_path)
        except Exception as _lk_log_exc:
            logger.warning("LIVEKIT_LOG_FILE_FAILED error=%s usando DEVNULL", _lk_log_exc)
            _livekit_log_file = subprocess.DEVNULL
        _livekit_process = subprocess.Popen(
            [str(LIVEKIT_SERVER_PATH), "--dev"],
            cwd=str(LIVEKIT_SERVER_PATH.parent),
            creationflags=creationflags,
            stdout=_livekit_log_file,
            stderr=_livekit_log_file,
        )
        _livekit_started_here = True
        for _ in range(20):
            time.sleep(0.5)
            if is_livekit_running():
                logger.info(
                    "LIVEKIT_SERVER_STARTED port=%s pid=%s path=%s",
                    LIVEKIT_PORT,
                    getattr(_livekit_process, "pid", None),
                    LIVEKIT_SERVER_PATH,
                )
                return True
        logger.error("LIVEKIT_SERVER_START_TIMEOUT port=%s", LIVEKIT_PORT)
        stop_livekit_server()
        return False
    except Exception:
        logger.exception("LIVEKIT_SERVER_START_FAILED path=%s", LIVEKIT_SERVER_PATH)
        stop_livekit_server()
        return False


def ensure_livekit_server_ready() -> bool:
    """Garante que o provider LiveKit selecionado esteja pronto para o worker."""
    boot_orchestrator.set_phase(BootPhase.LIVEKIT_STARTING, details="ensure_livekit_server_ready")
    try:
        provider = _select_and_apply_livekit_provider(force_refresh=True)
    except LiveKitProviderError as exc:
        message = str(exc)
        _update_livekit_runtime_state(
            livekit_status="all_failed",
            livekit_message=message,
            log_reason="provider_selection_failed",
        )
        _update_runtime_readiness(
            livekit_status="all_failed",
            livekit_ready=False,
            livekit_url="",
            ui_status="error",
            event="LIVEKIT_PROVIDER_FAILED",
            message=message,
            error=message,
            failure_reason=message,
        )
        boot_orchestrator.set_flag("livekit_server_ok", False)
        boot_orchestrator.fail(message)
        return False

    if is_cloud_provider(provider):
        logger.info("LIVEKIT_LOCAL_START_SKIPPED reason=cloud_provider_selected")
        _update_livekit_runtime_state(
            livekit_status="ready",
            livekit_message=f"LiveKit Cloud pronto ({provider.name}).",
            log_reason="cloud_provider_selected",
        )
        _update_runtime_readiness(
            livekit_status="ready",
            livekit_ready=True,
            livekit_url=provider.url,
            livekit_ready_source="cloud_provider_selected",
            event="LIVEKIT_PROVIDER_READY",
        )
        logger.info("LIVEKIT_PROVIDER_READY name=%s kind=%s url=%s", provider.name, provider.kind, provider.url)
        boot_orchestrator.set_flag("livekit_server_ok", True)
        boot_orchestrator.set_phase(BootPhase.LIVEKIT_READY, details=f"{provider.name}:{provider.kind}")
        get_runtime_status_manager().set_livekit_ready(True)
        return True

    logger.info("LIVEKIT_LOCAL_START_REQUIRED reason=local_provider_selected")
    if is_livekit_running():
        logger.info("LIVEKIT_READY port=%s", LIVEKIT_PORT)
        logger.info("[LIVEKIT_SERVER_READY] port=%s", LIVEKIT_PORT)
        logger.info("[BACKEND] LiveKit local verificado host=127.0.0.1 port=%s", LIVEKIT_PORT)
        _update_livekit_runtime_state(
            livekit_status="ready",
            livekit_message="LiveKit local pronto.",
            log_reason="livekit_verified",
        )
        _update_runtime_readiness(
            livekit_status="ready",
            livekit_ready=True,
            livekit_url=provider.url,
            event="LIVEKIT_READY_PUBLISHED",
        )
        logger.info("[LIVEKIT_READY_PUBLISHED] livekit_status=ready url=%s", provider.url)
        boot_orchestrator.set_flag("livekit_server_ok", True)
        boot_orchestrator.set_phase(BootPhase.LIVEKIT_READY, details="livekit_already_running")
        get_runtime_status_manager().set_livekit_ready(True)
        return True

    logger.warning(
        "[BACKEND] LiveKit nao responde em 127.0.0.1:%s. Tentando iniciar servidor local...",
        LIVEKIT_PORT,
    )
    if not start_livekit_server():
        logger.error("[BACKEND] LiveKit indisponivel host=127.0.0.1 port=%s", LIVEKIT_PORT)
        boot_orchestrator.set_flag("livekit_server_ok", False)
        boot_orchestrator.fail(f"LiveKit indisponivel em 127.0.0.1:{LIVEKIT_PORT}")
        return False

    if is_livekit_running():
        logger.info("[LIVEKIT_SERVER_READY] port=%s", LIVEKIT_PORT)
        logger.info("[BACKEND] LiveKit local iniciado e acessivel host=127.0.0.1 port=%s", LIVEKIT_PORT)
        _update_livekit_runtime_state(
            livekit_status="ready",
            livekit_message="LiveKit local pronto.",
            log_reason="livekit_started_here",
        )
        _update_runtime_readiness(
            livekit_status="ready",
            livekit_ready=True,
            livekit_url=provider.url,
            event="LIVEKIT_READY_PUBLISHED",
        )
        logger.info("[LIVEKIT_READY_PUBLISHED] livekit_status=ready url=%s", provider.url)
        boot_orchestrator.set_flag("livekit_server_ok", True)
        boot_orchestrator.set_phase(BootPhase.LIVEKIT_READY, details="livekit_started_here")
        get_runtime_status_manager().set_livekit_ready(True)
        return True

    logger.error("[BACKEND] LiveKit nao ficou acessivel host=127.0.0.1 port=%s", LIVEKIT_PORT)
    boot_orchestrator.set_flag("livekit_server_ok", False)
    boot_orchestrator.fail(f"LiveKit nao ficou acessivel em 127.0.0.1:{LIVEKIT_PORT}")
    return False


def _summarize_ready_probe_body(body_text: str) -> str:
    normalized = str(body_text or "").replace("\r", " ").replace("\n", " ").strip()
    if not normalized:
        return "<empty>"
    if len(normalized) > 80:
        return normalized[:77] + "..."
    return normalized


def _probe_livekit_worker_http_ready(
    url: str = LIVEKIT_WORKER_HTTP_URL,
    *,
    expected_proc=None,
) -> tuple[bool, str]:
    normalized_url = _normalize_ready_probe_url(url)
    try:
        if expected_proc is not None:
            poll_result = expected_proc.poll()
            if poll_result is not None:
                logger.error("[KIRA READY PROBE] agent_process died exit_code=%s", poll_result)
                return False, f"process_died:{poll_result}"

        req = urllib_request.Request(normalized_url, method="GET")
        with urllib_request.urlopen(req, timeout=10) as response:
            status = int(getattr(response, "status", 0) or 0)
            body_text = response.read(256).decode("utf-8", errors="replace").strip()
            body_summary = _summarize_ready_probe_body(body_text)

            if status != 200:
                return False, f"status={status} body={body_summary}"
            if not body_text or body_text.upper() == "OK":
                return True, f"status={status} body={body_summary}"

            parsed_json = None
            try:
                parsed_json = json.loads(body_text)
            except Exception:
                parsed_json = None

            if isinstance(parsed_json, dict):
                ready_flag = parsed_json.get("ready")
                status_flag = str(parsed_json.get("status") or "").strip().lower()
                health_flag = str(parsed_json.get("health") or "").strip().lower()
                if ready_flag is True or status_flag in {"ok", "ready", "healthy"} or health_flag in {"ok", "healthy"}:
                    return True, f"status={status} body={body_summary}"

            return False, f"unexpected_body={body_summary}"
    except urllib_error.HTTPError as exc:
        body_summary = "<empty>"
        try:
            body_summary = _summarize_ready_probe_body(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            pass
        return False, f"http_error={getattr(exc, 'code', 'unknown')} body={body_summary}"
    except urllib_error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        reason_text = str(reason or exc).strip() or exc.__class__.__name__
        return False, f"url_error={reason_text}"
    except socket.timeout:
        return False, "timeout"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{exc}"


def is_livekit_worker_http_ready(url: str = LIVEKIT_WORKER_HTTP_URL) -> bool:
    """Confirma que o worker HTTP subiu e responde OK."""
    ready, _ = _probe_livekit_worker_http_ready(url)
    return ready


def _kill_port_8081_if_occupied() -> None:
    """Libera a porta 8081 antes de subir o worker LiveKit da Kira."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            occupied = sock.connect_ex(("127.0.0.1", 8081)) == 0
        if not occupied:
            logger.info("[PORT_8081_FREE] Porta 8081 esta livre")
            return

        logger.warning("[PORT_8081_OCCUPIED] Porta 8081 ja esta em uso. Procurando processo...")
        import psutil

        found_listener = False
        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if not laddr or int(getattr(laddr, "port", 0) or 0) != 8081:
                continue
            if str(getattr(conn, "status", "") or "").upper() != "LISTEN":
                continue
            pid = getattr(conn, "pid", None)
            if not pid:
                continue
            found_listener = True
            try:
                proc = psutil.Process(pid)
                proc_name = proc.name()
                proc_cmdline = " ".join(proc.cmdline())
                is_kira_python = (
                    "python" in proc_name.lower()
                    and ("agent.py" in proc_cmdline or "kira" in proc_cmdline.lower())
                )
                if not is_kira_python:
                    logger.error(
                        "[PORT_8081_OTHER_PROCESS] Porta 8081 ocupada por processo nao-Kira pid=%s name=%s cmd=%s",
                        pid,
                        proc_name,
                        proc_cmdline[:240],
                    )
                    raise RuntimeError(f"Porta 8081 ocupada por processo externo: {proc_name} (PID {pid})")

                logger.warning("[PORT_8081_KILL] Matando worker zumbi pid=%s name=%s", pid, proc_name)
                proc.kill()
                proc.wait(timeout=5)
                logger.info("[PORT_8081_KILLED] Worker zumbi eliminado pid=%s", pid)

                for attempt in range(10):
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check_sock:
                        check_sock.settimeout(0.5)
                        if check_sock.connect_ex(("127.0.0.1", 8081)) != 0:
                            logger.info("[PORT_8081_FREE] Porta 8081 liberada apos %s tentativas", attempt + 1)
                            return
                    time.sleep(0.5)
                logger.warning("[PORT_8081_STILL_OCCUPIED] Porta ainda ocupada apos kill")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                logger.error("[PORT_8081_KILL_ERROR] Erro ao matar processo pid=%s: %s", pid, exc)
            break

        if not found_listener:
            logger.warning("[PORT_8081_OCCUPIED_NO_LISTENER] Porta respondeu, mas listener nao foi identificado")
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error("[PORT_8081_CHECK_ERROR] Erro ao verificar porta 8081: %s", exc)


def stop_livekit_server() -> None:
    """Encerra o LiveKit local se ele tiver sido iniciado pela Kira."""
    global _livekit_process, _livekit_started_here
    if not _livekit_started_here or _livekit_process is None:
        return
    try:
        _livekit_process.terminate()
        _livekit_process.wait(timeout=5)
        logger.info("LIVEKIT_SERVER_STOPPED pid=%s", getattr(_livekit_process, "pid", None))
    except Exception:
        try:
            _livekit_process.kill()
        except Exception:
            pass
    finally:
        _livekit_process = None
        _livekit_started_here = False

def _get_console_hwnd():
    return ctypes.windll.kernel32.GetConsoleWindow()

def hide_console():
    hwnd = _get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE

def show_console():
    hwnd = _get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ctypes.windll.user32.SetForegroundWindow(hwnd)


def _set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        pass


def _apply_windows_icon_to_window() -> None:
    if os.name != "nt" or not APP_ICON_PATH.exists():
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Kira")
        if not hwnd:
            return
        icon_handle = user32.LoadImageW(
            None,
            str(APP_ICON_PATH),
            1,  # IMAGE_ICON
            0,
            0,
            0x00000010,  # LR_LOADFROMFILE
        )
        if not icon_handle:
            return
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, icon_handle)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, icon_handle)
    except Exception:
        pass

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  CONFIGURAÃ‡ÃƒO â€” ajuste esses caminhos
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Pasta onde estÃ¡ o agente (agent.py)
BASE_DIR = Path(__file__).resolve().parent
INTERFACE_DIR = BASE_DIR / "interface"
AGENT_DIR = BASE_DIR / "agent"
APP_ASSETS_DIR = BASE_DIR / "assets"
APP_ICONS_DIR = APP_ASSETS_DIR / "icons"
INTERFACE_ASSETS_DIR = INTERFACE_DIR / "assets"
INTERFACE_ICONS_DIR = INTERFACE_ASSETS_DIR / "icons"
APP_ICON_PATH = APP_ICONS_DIR / "kira.ico"
WINDOWS_APP_ID = "Kira.Desktop.App"

# Inicializacao fixa: token e dispatch preparados antes do botao. O processo filho
# herda estes valores antes de calcularmos os flags de boot.
os.environ.update(
    {
        "KIRA_PREPARE_SESSION_BEFORE_BUTTON": "1",
        "KIRA_SESSION_PREPARE_DISPATCH": "1",
        "KIRA_LIVEKIT_NUM_IDLE_PROCESSES": "0",
        "KIRA_UI_VOICE_ATTACH_LATE": "0",
    }
)

# Comando pra iniciar o agente (relativo a AGENT_DIR)
AGENT_MODE = str(os.getenv("KIRA_AGENT_MODE", "start") or "start").strip().lower()
if AGENT_MODE not in {"start", "dev", "console", "worker"}:
    AGENT_MODE = "start"
BOOT_PYTHON_EXE = str(os.getenv("KIRA_PYTHON_EXE") or sys.executable).strip() or sys.executable
AGENT_CMD = [BOOT_PYTHON_EXE, "agent.py", AGENT_MODE]


def _official_venv_python_exe() -> str:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return str(AGENT_DIR / "venv" / scripts_dir / executable)


def _same_executable_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except Exception:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _enforce_official_boot_python() -> None:
    expected = _official_venv_python_exe()
    running = sys.executable
    selected = BOOT_PYTHON_EXE
    if _same_executable_path(running, expected) and _same_executable_path(selected, expected):
        logger.info("KIRA_PYTHON_SELECTED origin=venv path=%s", running)
        return
    logger.error(
        "KIRA_PYTHON_GLOBAL_FALLBACK_BLOCKED reason=non_official_python running=%s selected=%s expected=%s",
        running,
        selected,
        expected,
    )
    print("KIRA_PYTHON_GLOBAL_FALLBACK_BLOCKED reason=non_official_python")
    print(f"running={running}")
    print(f"selected={selected}")
    print(f"expected={expected}")
    sys.exit(1)

# Bridge do WhatsApp: Node + Baileys (protocolo do WhatsApp Web, sem navegador). Caminho absoluto na linha de
# comando para a limpeza do boot reconhecer o processo como da Kira.
WHATSAPP_DIR = BASE_DIR / "whatsapp_bridge"
WHATSAPP_CMD = ["node", str(WHATSAPP_DIR / "bridge.mjs")]

# Caminho do .env
LOGS_DIR = BASE_DIR / "logs"
CONFIGS_DIR = BASE_DIR / "configs"
WEBVIEW_STORAGE_DIR = CONFIGS_DIR / "webview_storage"
WINDOW_STATE_PATH = CONFIGS_DIR / "window_state.json"
MUSIC_PLAYLISTS_PATH = CONFIGS_DIR / "music_playlists.json"
MUSIC_STATE_PATH = CONFIGS_DIR / "music_state.json"
ENV_PATH = BASE_DIR / ".env"
LEGACY_ENV_PATH = AGENT_DIR / ".env"
WHATSAPP_RUNTIME_CONFIG_PATH = CONFIGS_DIR / "whatsapp_runtime.json"
VOICE_RUNTIME_LOG_PATH = LOGS_DIR / "voice_runtime.log"
VOICE_VALIDATION_LOG_PATH = LOGS_DIR / "voice_validation.log"

# Porta da interface
PORT = 3000
BOOT_ROOM_TIMEOUT_SECONDS = float(os.getenv("KIRA_BOOT_ROOM_TIMEOUT_SECONDS", "35"))
BOOT_GEMINI_TIMEOUT_SECONDS = float(os.getenv("KIRA_BOOT_GEMINI_TIMEOUT_SECONDS", "45"))
BOOT_RUNTIME_TIMEOUT_SECONDS = float(os.getenv("KIRA_BOOT_RUNTIME_TIMEOUT_SECONDS", "70"))
BOOT_TOTAL_TIMEOUT_SECONDS = float(os.getenv("KIRA_BOOT_TOTAL_TIMEOUT_SECONDS", "120"))
AGENT_AUTO_RESTART_MAX_ATTEMPTS = max(
    int(os.getenv("KIRA_AGENT_AUTO_RESTART_MAX_ATTEMPTS", "5")),
    1,
)
AGENT_AUTO_RESTART_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_AGENT_AUTO_RESTART_COOLDOWN_SECONDS", "10")),
    1.0,
)
KIRA_DISPATCH_REFRESH_MAX_ATTEMPTS = max(int(os.getenv("KIRA_DISPATCH_REFRESH_MAX_ATTEMPTS", "2")), 1)
KIRA_SESSION_AGENT_JOIN_WAIT_SECONDS = max(float(os.getenv("KIRA_SESSION_AGENT_JOIN_WAIT_SECONDS", "2.0")), 0.5)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

load_dotenv(ENV_PATH if ENV_PATH.exists() else LEGACY_ENV_PATH)
KIRA_INTERNAL_TOKEN = secrets.token_urlsafe(32)
# o próprio backend também fala com a ponte do WhatsApp (envio rápido/confirmar): sem isto ia sem senha e levava 403
os.environ["KIRA_INTERNAL_TOKEN"] = KIRA_INTERNAL_TOKEN
# participante do celular na sala de voz; o agente troca para ele quando o microfone de lá abre (agent/agent.py)
PHONE_PARTICIPANT_IDENTITY = "kira-celular"
try:
    _select_and_apply_livekit_provider()
except LiveKitProviderError:
    # ensure_livekit_server_ready() publica o erro de readiness com mensagem clara.
    pass

LIVEKIT_URL        = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
AGENT_NAME         = os.getenv("LIVEKIT_AGENT_NAME", "").strip()
VISION_CONTROL_URL = (
    os.getenv("KIRA_VISION_CONTROL_URL")
    or f"http://127.0.0.1:{int(os.getenv('KIRA_VISION_CONTROL_PORT', '8092') or '8092')}"
).strip().rstrip("/")
if not AGENT_NAME:
    AGENT_NAME = "kira-agent"
LIVEKIT_MANAGER = LiveKitKeyManager(
    total_slots=4,
    state_file=LOGS_DIR / "livekit_state.json",
)
LIVEKIT_DISPATCH_STRATEGY = str(
    os.getenv("KIRA_LIVEKIT_DISPATCH_STRATEGY", "auto")
).strip().lower() or "auto"
if LIVEKIT_DISPATCH_STRATEGY not in {"auto", "room_config", "explicit", "both"}:
    LIVEKIT_DISPATCH_STRATEGY = "auto"
logger.info("[AGENT_NAME_BACKEND=%s]", AGENT_NAME)
logger.info("[LIVEKIT_DISPATCH_AGENT_NAME=%s]", AGENT_NAME)
logger.info("[AGENT_NAME_WORKER_EXPECTED=%s]", AGENT_NAME)

# â”€â”€ Processos globais â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_agent_proc           = None
_whatsapp_proc        = None
_webview_win          = None
_backend_loop         = None
_satellite_registry   = None
_browser_webview_win  = None
_tray_icon            = None
_browser_host_available = False
BROWSER_HOST_ENABLED = (
    os.getenv("KIRA_ENABLE_BROWSER_HOST", "0").strip().lower() in {"1", "true", "yes", "on"}
    and should_autoload("browser")
)
_server_ready = threading.Event()
_server_start_error: str | None = None
_agent_restart_count = 0
_agent_last_restart_at = 0.0
_agent_restart_policy = AgentRestartPolicy(time.time)
_agent_recovery_lock = threading.Lock()
_session_worker_lifecycle_lock = threading.Lock()
_session_stop_in_progress = threading.Event()
_post_boot_lazy_load_started = False
_post_boot_lazy_load_lock = threading.Lock()
_livekit_failed_slots_lock = threading.RLock()
_livekit_boot_failed_slots: set[int] = set()
_agent_expected_exit_pids: set[int] = set()
LIVEKIT_WORKER_EXIT_AUTH_FAILED = 21
LIVEKIT_WORKER_EXIT_QUOTA_FAILED = 22

KIRA_FEATURES = load_kira_features()
KIRA_OPTIONAL_MODULES = get_optional_modules()
logger.info("KIRA_CORE_MODE_ENABLED")
for _optional_name, _optional_cfg in KIRA_OPTIONAL_MODULES.items():
    if not bool(_optional_cfg.get("enabled")):
        logger.info("OPTIONAL_MODULE_DISABLED module=%s reason=config", _optional_name)

_kb = (
    safe_import_optional("browser.kira_browser", service="browser")
    if should_autoload("browser")
    else None
)
from agent.gemini_key_manager import (
    GeminiKeyManager,
    activate_current_gemini_api_key,
    mark_current_gemini_key_quota_exceeded,
    mark_current_gemini_key_working,
    rotate_to_next_usable_gemini_api_key,
)
from agent.runtime.process_tracker import get_tracker
_youtube_music_module = (
    safe_import_optional("services.youtube_music_service", service="music")
    if should_autoload("music")
    else None
)
_whatsapp_data_module = (
    safe_import_optional("services.whatsapp_data", service="whatsapp")
    if is_optional_enabled("whatsapp")
    else None
)
_system_vitals_module = None

MusicServiceError = getattr(_youtube_music_module, "MusicServiceError", RuntimeError)
MusicStateStore = getattr(_youtube_music_module, "MusicStateStore", None)
PlaylistStore = getattr(_youtube_music_module, "PlaylistStore", None)
YouTubeMusicService = getattr(_youtube_music_module, "YouTubeMusicService", None)
load_whatsapp_contacts = getattr(_whatsapp_data_module, "load_whatsapp_contacts", lambda: {"contacts": []})
load_whatsapp_send_records = getattr(_whatsapp_data_module, "load_whatsapp_send_records", lambda limit=80: [])
resolve_whatsapp_contact = getattr(_whatsapp_data_module, "resolve_whatsapp_contact", lambda query: None)
upsert_whatsapp_contact = getattr(
    _whatsapp_data_module,
    "upsert_whatsapp_contact",
    lambda payload: {**dict(payload or {}), "error": "whatsapp_data_unavailable"},
)
def _get_system_vitals_module():
    global _system_vitals_module
    if _system_vitals_module is None:
        if not is_optional_enabled("system_vitals"):
            logger.info("SYSTEM_VITALS_DISABLED_SKIPPED reason=config source=lazy_import")
            return None
        _system_vitals_module = safe_import_optional("services.system_vitals", service="system_vitals")
    return _system_vitals_module


def get_system_vitals(*, min_interval_seconds: float = 10.0, force: bool = False, scope: str = "global"):
    module = _get_system_vitals_module()
    if module is None:
        return optional_status_payload("system_vitals")
    getter = getattr(module, "get_system_vitals_cached", None) or getattr(module, "get_system_vitals", None)
    if callable(getter):
        try:
            return getter(min_interval_seconds=min_interval_seconds, force=force, scope=scope)
        except TypeError:
            return getter()
    return {"ok": False, "degraded": True, "error": "system_vitals_unavailable", "timestamp": time.time()}


def get_process_vitals(name: str):
    module = _get_system_vitals_module()
    if module is None:
        return optional_status_payload("system_vitals")
    getter = getattr(module, "get_process_vitals", None)
    if callable(getter):
        return getter(name)
    return {"enabled": True, "matched": False, "message": "process vitals unavailable", "sampled_at": time.time()}
# TEMPORARIAMENTE DESABILITADO - Sistema de contatos
# from services.contacts_manager import get_contacts_manager

# Persistent HTTP client for browser proxy (connection pooling + HTTP/2)
import httpx as _httpx
_browser_http_client: _httpx.AsyncClient | None = None
tracker = get_tracker()


def _collect_subprocess_health_snapshot() -> dict:
    try:
        process_map = {
            "livekit": _livekit_process,
            "agent": _agent_proc,
            "whatsapp": _whatsapp_proc,
        }
        processes = inspect_processes(process_map)
        logger.info("BLOCO7_PROCESS_HEALTH_SNAPSHOT_READY count=%s", len(processes))
        for name, snapshot in processes.items():
            if bool(snapshot.get("crashed")):
                logger.error(
                    "BLOCO7_PROCESS_CRASH_DETECTED name=%s pid=%s exit_code=%s",
                    name,
                    snapshot.get("pid"),
                    snapshot.get("exit_code"),
                )
        return {
            "ok": True,
            "processes": processes,
            "checked_at": time.time(),
        }
    except Exception as exc:
        logger.warning("BLOCO7_PROCESS_HEALTH_SNAPSHOT_FAILED error=%s", exc)
        return {
            "ok": False,
            "processes": {},
            "checked_at": time.time(),
            "error": str(exc),
        }


async def _get_browser_client() -> _httpx.AsyncClient:
    global _browser_http_client
    if _browser_http_client is None or _browser_http_client.is_closed:
        try:
            import h2  # noqa: F401
            _use_h2 = True
        except ImportError:
            _use_h2 = False
        _browser_http_client = _httpx.AsyncClient(
            follow_redirects=True,
            timeout=12.0,
            verify=False,
            http2=_use_h2,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                # Explicitly avoid brotli — only request gzip/deflate which httpx handles natively.
                # Sending 'br' without the brotli package installed causes garbled binary output.
                "Accept-Encoding": "gzip, deflate",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            },
        )
    return _browser_http_client


async def _agent_vision_api_call(
    method: str,
    path: str,
    payload: dict | None = None,
) -> dict:
    url = f"{VISION_CONTROL_URL}{path}"
    timeout = _httpx.Timeout(0.7)
    async with _httpx.AsyncClient(timeout=timeout) as client:
        if method == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url, json=payload or {})

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code >= 400 or not isinstance(data, dict) or data.get("success") is False:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("detail") or "").strip()
        if not detail:
            detail = f"agent_vision_api_http_{response.status_code}"
        raise RuntimeError(detail)

    return data


async def _get_agent_vision_state() -> dict:
    from services.vision_livekit_state import get_vision_livekit_state

    state = get_vision_livekit_state()
    return _apply_effective_vision_state(state, status_failed=False)


def _apply_effective_vision_state(state: dict, *, status_failed: bool) -> dict:
    now = time.time()
    stale_ms = int(os.getenv("KIRA_VISION_FRAME_STALE_MS", "5000") or "5000")
    failure_tolerance = int(os.getenv("KIRA_VISION_STATUS_FAILURE_TOLERANCE", "5") or "5")
    frame_age = state.get("latest_frame_age_ms")
    latest_frame_age_ms = int(frame_age) if frame_age is not None else None
    recent_frame = latest_frame_age_ms is not None and latest_frame_age_ms <= stale_ms
    if not status_failed:
        _vision_effective_state_cache["last_successful_status_at"] = now
        _vision_effective_state_cache["status_failure_count"] = 0
        _vision_effective_state_cache["last_payload"] = dict(state)
        if recent_frame:
            _vision_effective_state_cache["last_frame_at"] = now - (latest_frame_age_ms / 1000.0)
    else:
        _vision_effective_state_cache["status_failure_count"] = int(
            _vision_effective_state_cache.get("status_failure_count") or 0
        ) + 1
        last_frame_at = float(_vision_effective_state_cache.get("last_frame_at") or 0.0)
        recent_frame = bool(last_frame_at and (now - last_frame_at) * 1000.0 <= stale_ms)
        latest_frame_age_ms = int((now - last_frame_at) * 1000.0) if recent_frame else None
        cached = dict(_vision_effective_state_cache.get("last_payload") or {})
        if cached:
            state = {**cached, **state}
            state["latest_frame_age_ms"] = latest_frame_age_ms
    track_active = bool(state.get("screen_share_track_active") or state.get("visual_track_active") or state.get("track_active"))
    native_video_active = bool(state.get("native_video_active") or state.get("livekit_screen_native_video_active"))
    livekit_active = bool(state.get("livekit_screen_active")) or str(state.get("active_source") or "") == "livekit_screen_share"
    screen_requested = bool(state.get("screen_share_requested") or state.get("vision_mode_enabled") or livekit_active)
    active_signal = bool(state.get("vision_mode_enabled") or state.get("screen_capture_active") or livekit_active or track_active or native_video_active)
    effective_active = bool(
        active_signal
        and (
            recent_frame
            or (recent_frame and state.get("active_monitor_id") is not None)
        )
    )
    if screen_requested and not recent_frame:
        effective_active = False
        state["vision_status"] = state.get("vision_status") or "waiting_frame"
        state["state"] = state.get("state") or "waiting_frame"
        state["reason"] = "requested_but_no_frame"
        logger.info("VISION_WAITING_FRAME reason=requested_but_no_frame")
        logger.info("VISION_STATUS_RESPONSE state=requested_no_frame")
        if track_active:
            logger.info("VISION_NOT_DISABLED_REASON=track_active")
            logger.info("VISION_NO_RECENT_FRAME_BUT_TRACK_ACTIVE")
            logger.info("VISION_STATE_SYNCED_WITH_LIVEKIT_TRACK")
    if status_failed and recent_frame and int(_vision_effective_state_cache.get("status_failure_count") or 0) <= failure_tolerance:
        logger.warning(
            "VISION_STATUS_FAILURE_IGNORED recent_frame=true count=%s",
            _vision_effective_state_cache.get("status_failure_count"),
        )
        effective_active = True
    elif status_failed and int(_vision_effective_state_cache.get("status_failure_count") or 0) < failure_tolerance:
        logger.warning(
            "VISION_STATUS_FAILURE_DEGRADED count=%s",
            _vision_effective_state_cache.get("status_failure_count"),
        )
    elif status_failed:
        logger.warning(
            "VISION_STATUS_FAILURE_CONFIRMED_INACTIVE count=%s",
            _vision_effective_state_cache.get("status_failure_count"),
        )
    if effective_active and not bool(state.get("livekit_screen_active")) and recent_frame:
        logger.info("VISION_EFFECTIVE_ACTIVE_FROM_RECENT_FRAME")
    state["effective_active"] = effective_active
    state["recent_frame"] = recent_frame
    state["status_failed"] = bool(status_failed)
    state["screen_share_requested"] = screen_requested
    state["track_active"] = track_active
    state["native_video_active"] = native_video_active
    state["frame_store_available"] = bool(state.get("frames_available") or state.get("first_frame_received"))
    state["vision_frames_recent"] = recent_frame
    state["vision_effective_active"] = effective_active
    state["reason"] = state.get("reason") or ("recent_frame" if effective_active else ("status_failed" if status_failed else "inactive_or_stale"))
    logger.info(
        "VISION_EFFECTIVE_STATE active=%s recent_frame=%s monitor=%s reason=%s",
        bool(state.get("vision_mode_enabled")),
        recent_frame,
        state.get("active_monitor_id"),
        state.get("reason"),
    )
    return state


def _is_pc_operator_executing_from_status() -> bool:
    try:
        status_path = Path(__file__).resolve().parent / "runtime" / "desktop_operator_status.json"
        if not status_path.exists():
            return False
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        return str((payload or {}).get("status") or "").strip().lower() in {"executing", "verifying"}
    except Exception:
        return False


async def _get_agent_vision_monitors() -> dict:
    from services.vision_livekit_state import legacy_vision_blocked_response

    data = legacy_vision_blocked_response(reason="monitors_disabled_livekit_only")
    monitors = []
    return {
        "monitors": monitors,
        "count": int(data.get("count") or len(monitors)),
        "provider": "livekit",
        "legacy_providers_disabled": True,
        "requires_ui_publish": True,
    }


async def _toggle_agent_vision_mode(
    enabled: bool,
    *,
    source: str = "manual",
    monitor_id: int | None = None,
) -> dict:
    from services.vision_livekit_state import start_vision_livekit, stop_vision_livekit

    reason = "user_enabled" if enabled else "user_disabled"
    if enabled:
        return start_vision_livekit(reason=reason)
    return stop_vision_livekit(reason=reason)


async def _ping_agent_activity(source: str) -> dict:
    data = await _agent_vision_api_call(
        "POST",
        "/activity/ping",
        {"source": str(source or "unknown").strip() or "unknown"},
    )
    return {
        "vision_mode_enabled": bool(data.get("vision_mode_enabled")),
        "vision_enabled": bool(data.get("vision_enabled", data.get("vision_mode_enabled"))),
        "screen_capture_active": bool(data.get("screen_capture_active")),
        "screen_capture_enabled": bool(data.get("screen_capture_enabled")),
        "screen_capture_last_error": data.get("screen_capture_last_error"),
        "synchronized": bool(data.get("synchronized")),
        "monitor_count": int(data.get("monitor_count") or 0),
        "captured_monitor_count": int(data.get("captured_monitor_count") or 0),
        "active_monitor_id": (
            int(data.get("active_monitor_id"))
            if data.get("active_monitor_id") is not None
            else None
        ),
        "last_frame_time": data.get("last_frame_time"),
        "idle_time_seconds": data.get("idle_time_seconds"),
        "last_activity_source": data.get("last_activity_source"),
        "visual_processing_active": bool(data.get("visual_processing_active")),
        "active_source": str(data.get("active_source") or "none"),
        "livekit_screen_active": bool(data.get("livekit_screen_active")),
        "dxcam_active": bool(data.get("dxcam_active")),
        "latest_frame_age_ms": data.get("latest_frame_age_ms"),
        "latest_frame_size": data.get("latest_frame_size"),
    }
_agent_ready  = threading.Event()  # sinaliza quando o worker LiveKit estÃ¡ pronto
_whatsapp_lock = threading.RLock()
_whatsapp_runtime_state = {
    "state": "disabled",
    "last_error": None,
    "bridge_available": False,
    "updated_at": None,
}
_whatsapp_healthcheck_started = False
_whatsapp_autostart_done = False
_assistant_lock = threading.RLock()
_assistant_runtime_state = {
    "mode_enabled": False,
    "screen_share_active": False,
    "control_enabled": False,
    "updated_at": None,
}
_vision_effective_state_cache = {
    "last_frame_at": 0.0,
    "last_successful_status_at": 0.0,
    "status_failure_count": 0,
    "last_payload": {},
    "pending_stop_since": 0.0,
}
_assistant_bridge = None
_ui_bridge_lock = threading.RLock()
_kira_ui_last_state = {
    "active_tab": "kira",
    "mic_enabled": False,
    "camera_enabled": False,
    "screen_enabled": False,
    "session_connected": False,
    "updated_at": None,
}

logger.debug("PC_OPERATOR_REMOVED")  # [REMOVED] PC_OPERATOR: downgraded to debug
_runtime_lock = threading.RLock()
_music_service = None
_music_playlists = None
_music_state = None
_youtube_audio_service = None
_music_fallback_service = None
_music_components_loaded = False
_text_only_chat_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="kira-text-only",
)
_text_only_chat_lock = threading.RLock()
_text_only_chat_history: list[dict[str, str]] = []
TEXT_ONLY_GEMINI_MODEL = os.getenv("KIRA_GEMINI_TEXT_ONLY_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
TEXT_ONLY_MAX_HISTORY_MESSAGES = max(int(os.getenv("KIRA_TEXT_ONLY_MAX_HISTORY_MESSAGES", "12")), 2)
TEXT_ONLY_MAX_GEMINI_ROTATIONS = max(int(os.getenv("KIRA_TEXT_ONLY_MAX_GEMINI_ROTATIONS", "6")), 1)
# Timeout total do endpoint de chat texto (aguarda a thread do executor).
TEXT_ONLY_CHAT_TIMEOUT_SECONDS = max(float(os.getenv("KIRA_TEXT_ONLY_CHAT_TIMEOUT_SECONDS", "45")), 5.0)
# Timeout por tentativa HTTP do Gemini (ms) — garante que a thread nao trave para sempre
# bloqueando o executor de worker unico. Mantido abaixo do timeout total para permitir rotacao.
TEXT_ONLY_CHAT_HTTP_TIMEOUT_MS = max(
    int(min(TEXT_ONLY_CHAT_TIMEOUT_SECONDS, 25.0) * 1000),
    5000,
)
TEXT_ONLY_SYSTEM_PROMPT = get_kira_style_rules("text_only")


def _ui_state_now() -> dict:
    snapshot = dict(_kira_ui_last_state)
    snapshot["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _kira_ui_last_state.update(snapshot)
    return snapshot


def _evaluate_js_json(script: str) -> dict:
    with _ui_bridge_lock:
        if _webview_win is None:
            raise RuntimeError("webview_not_ready")
        raw = _webview_win.evaluate_js(script)
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("empty_js_result")
    try:
        return json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"invalid_js_json: {text}") from exc


def _read_kira_ui_state() -> dict:
    try:
        data = _evaluate_js_json(
            "(function(){ const s = (window.KiraUiControl && window.KiraUiControl.getState) ? window.KiraUiControl.getState() : null; return JSON.stringify(s || {}); })();"
        )
        if isinstance(data, dict) and data:
            _kira_ui_last_state.update(data)
        return _ui_state_now()
    except Exception:
        return _ui_state_now()


def _browser_host_ready() -> bool:
    return bool(_kb is not None and _browser_host_available and BROWSER_HOST_ENABLED)


# Com o celular como aparelho ativo, música e vídeo tocam lá: build_app() liga isto ao hub de ações (janela "celular").
_PHONE_MEDIA_ROUTE = None  # (comando, valor) -> entregou no celular?


def _dispatch_kira_ui_action(payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower()
    resource = str(payload.get("resource") or "").strip().lower()
    enabled = payload.get("enabled", None)
    target = str(payload.get("target") or "").strip().lower()
    before = _read_kira_ui_state()

    if action not in {"set_session_control", "open_tab", "music_command"}:
        return {"ok": False, "error": "invalid_action", "state_before": before, "state_after": before}

    if action == "open_tab":
        target = {
            "home": "kira",
            "inicio": "kira",
            "principal": "kira",
            "system": "kira",
            "sistema": "kira",
            "settings": "config",
        }.get(target, target)
        sub = ""  # "abre as configurações na parte de economia" -> "config:economy" (2026-11-07)
        if target.startswith("config:"):
            target, sub = "config", target.split(":", 1)[1]
            sub = {"economia": "economy", "som": "audio", "aparencia": "visual", "personalidade": "personality", "memoria": "memory",
                   "remoto": "remote"}.get(sub, sub)
            sub = sub if sub in {"audio", "economy", "visual", "kira", "personality", "memory", "remote"} else ""
        if target not in {"kira", "whatsapp", "config", "browser", "music"}:
            return {"ok": False, "error": "tab_not_found", "state_before": before, "state_after": before}
        if _PHONE_MEDIA_ROUTE is not None and _PHONE_MEDIA_ROUTE("open_tab", target):
            return {"ok": True, "device": "celular", "state_before": before, "state_after": before}
        tab = f"{target}:{sub}" if sub else target
        js = f"(function(){{ if(window.KiraUiControl && window.KiraUiControl.openTab) return JSON.stringify(window.KiraUiControl.openTab('{tab}')); return JSON.stringify({{ok:false,error:'bridge_not_available'}}); }})();"
        result = _evaluate_js_json(js)
    elif action == "music_command":
        command = str(payload.get("command") or "").strip().lower()
        value = payload.get("value", None)
        if not command:
            return {"ok": False, "error": "music_command_required", "state_before": before, "state_after": before}
        if _PHONE_MEDIA_ROUTE is not None and _PHONE_MEDIA_ROUTE(command, value):
            # o Chefe está no celular: toca lá (services/remote_devices.py)
            return {"ok": True, "device": "celular", "state_before": before, "state_after": before}
        value_json = json.dumps(value)
        js = (
            "(function(){"
            f" if(window.KiraUiControl && window.KiraUiControl.musicCommand) return JSON.stringify(window.KiraUiControl.musicCommand('{command}', {value_json}));"
            " return JSON.stringify({ok:false,error:'bridge_not_available'});"
            "})();"
        )
        result = _evaluate_js_json(js)
    else:
        if enabled is None:
            return {"ok": False, "error": "enabled_required", "state_before": before, "state_after": before}
        if resource not in {"mic", "webcam", "screen_share", "transcription"}:
            return {"ok": False, "error": "resource_not_supported", "state_before": before, "state_after": before}
        resource_method = {
            "mic": "setMicEnabled",
            "webcam": "setCameraEnabled",
            "screen_share": "setScreenEnabled",
            "transcription": "setTranscriptionEnabled",
        }[resource]
        enabled_js = "true" if bool(enabled) else "false"
        js = (
            "(function(){"
            f" if(window.KiraUiControl && window.KiraUiControl.{resource_method}) return JSON.stringify(window.KiraUiControl.{resource_method}({enabled_js}));"
            " return JSON.stringify({ok:false,error:'bridge_not_available'});"
            "})();"
        )
        result = _evaluate_js_json(js)

    after = _read_kira_ui_state()
    ok = bool(result.get("ok"))
    if ok and action == "set_session_control" and resource == "screen_share" and bool(enabled) and result.get("pending"):
        # Ligar a visão abre o seletor (monitor/janela): o estado só muda quando o usuário escolher.
        return {
            "ok": True, "error": None, "pending_choice": True, "action": action, "resource": resource,
            "target": target or None, "requested_enabled": True, "state_before": before, "state_after": after,
        }
    if ok and action == "open_tab":
        ok = after.get("active_tab") == target
        if not ok:
            result["error"] = "tab_not_applied"
    if ok and action == "set_session_control":
        expected_map = {
            "mic": "mic_enabled",
            "webcam": "camera_enabled",
            "screen_share": "screen_enabled",
        }
        expected_key = expected_map.get(resource)
        if expected_key:
            ok = bool(after.get(expected_key)) == bool(enabled)
            if not ok:
                result["error"] = "state_not_applied"

    if not ok and bool(result.get("ok")):
        deadline = time.monotonic() + 2.2
        while time.monotonic() < deadline:
            time.sleep(0.12)
            after = _read_kira_ui_state()
            if action == "open_tab":
                if after.get("active_tab") == target:
                    ok = True
                    result["error"] = None
                    break
            elif action == "set_session_control":
                expected_map = {
                    "mic": "mic_enabled",
                    "webcam": "camera_enabled",
                    "screen_share": "screen_enabled",
                    "transcription": None,
                }
                expected_key = expected_map.get(resource)
                if expected_key is None:
                    ok = True
                    result["error"] = None
                    break
                if bool(after.get(expected_key)) == bool(enabled):
                    ok = True
                    result["error"] = None
                    break

    return {
        "ok": ok,
        "error": result.get("error"),
        "action": action,
        "resource": resource or None,
        "target": target or None,
        "requested_enabled": bool(enabled) if enabled is not None else None,
        "state_before": before,
        "state_after": after,
    }


@dataclass
class RuntimeReadinessState:
    boot_state: str = "booting"
    ui_status: str = "connecting"
    agent_process_spawned: bool = False
    agent_process_dead: bool = False
    agent_connected: bool = False
    agent_ready: bool = False
    agent_error: bool = False
    agent_last_error: str = ""
    last_agent_heartbeat_at: float = 0.0
    agent_heartbeat_age_ms: int = 0
    agent_heartbeat_ok: bool = False
    agent_heartbeat_status: str = "pending"
    agent_heartbeat_stale_since: float = 0.0
    agent_status: str = "starting"
    room_connecting: bool = False
    room_ready: bool = False
    gemini_connecting: bool = False
    gemini_ready: bool = False
    session_started: bool = False
    handlers_registered: bool = False
    voice_pipeline_ready: bool = False
    runtime_ready: bool = False
    ui_ready: bool = False
    backend_ready: bool = False
    backend_status: str = "starting"
    backend_url: str = ""
    backend_ready_source: str = "unknown"
    memory_ready: bool = False
    memory_status: str = "unknown"
    memory_error: str = ""
    memory_checked_at: float = 0.0
    memory_elapsed_ms: int | None = None
    startup_greeting_created: bool = False
    startup_greeting_sanitized: bool = False
    startup_greeting_enqueued: bool = False
    startup_greeting_dispatched: bool = False
    startup_greeting_speaking: bool = False
    startup_greeting_done: bool = False
    functional_ready: bool = False
    boot_recovery_active: bool = False
    room_connected: bool = False
    agent_state: str = "idle"
    user_state: str = "idle"
    requested_room_name: str = ""
    room_name: str = ""
    livekit_current_slot: int | None = None
    livekit_total_slots: int = LIVEKIT_MANAGER.total_slots
    livekit_status: str = "starting"
    livekit_worker_boot_status: str = "idle"
    livekit_ready_source: str = "unknown"
    livekit_ready: bool = False
    livekit_url: str = ""
    voice_mode: str = "voice"
    restart_required_for_voice: bool = False
    livekit_message: str = "LiveKit aguardando sessao."
    event: str = ""
    message: str = "Inicializando runtime da Kira..."
    error: str = ""
    failure_reason: str = ""
    boot_started_at: float = field(default_factory=time.monotonic)
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def as_dict(self) -> dict:
        return {
            "boot_state": self.boot_state,
            "boot_phase": self.boot_state,
            "ui_status": self.ui_status,
            "agent_process_spawned": self.agent_process_spawned,
            "agent_process_dead": self.agent_process_dead,
            "agent_connected": self.agent_connected,
            "agent_ready": self.agent_ready,
            "agent_error": self.agent_error,
            "agent_last_error": self.agent_last_error,
            "last_agent_heartbeat_at": self.last_agent_heartbeat_at,
            "agent_heartbeat_age_ms": self.agent_heartbeat_age_ms,
            "agent_heartbeat_ok": self.agent_heartbeat_ok,
            "agent_heartbeat_status": self.agent_heartbeat_status,
            "agent_heartbeat_stale_since": self.agent_heartbeat_stale_since,
            "agent_status": self.agent_status,
            "room_connecting": self.room_connecting,
            "room_ready": self.room_ready,
            "gemini_connecting": self.gemini_connecting,
            "gemini_ready": self.gemini_ready,
            "session_started": self.session_started,
            "handlers_registered": self.handlers_registered,
            "voice_pipeline_ready": self.voice_pipeline_ready,
            "runtime_ready": self.runtime_ready,
            "ui_ready": self.ui_ready,
            "backend_ready": self.backend_ready,
            "backend_status": self.backend_status,
            "backend_url": self.backend_url,
            "backend_ready_source": self.backend_ready_source,
            "memory_ready": self.memory_ready,
            "memory_status": self.memory_status,
            "memory_error": self.memory_error,
            "memory_checked_at": self.memory_checked_at,
            "memory_elapsed_ms": self.memory_elapsed_ms,
            "startup_greeting_created": self.startup_greeting_created,
            "startup_greeting_sanitized": self.startup_greeting_sanitized,
            "startup_greeting_enqueued": self.startup_greeting_enqueued,
            "startup_greeting_dispatched": self.startup_greeting_dispatched,
            "startup_greeting_speaking": self.startup_greeting_speaking,
            "startup_greeting_done": self.startup_greeting_done,
            "functional_ready": self.functional_ready,
            "ui_session_ready": self.ui_ready,
            "boot_recovery_active": self.boot_recovery_active,
            "room_connected": self.room_connected,
            "agent_state": self.agent_state,
            "user_state": self.user_state,
            "requested_room_name": self.requested_room_name,
            "room_name": self.room_name,
            "livekit_current_slot": self.livekit_current_slot,
            "livekit_total_slots": self.livekit_total_slots,
            "livekit_key": (
                f"{self.livekit_current_slot if self.livekit_current_slot is not None else '-'}"
                f"/{self.livekit_total_slots}"
            ),
            "livekit_status": self.livekit_status,
            "livekit_worker_boot_status": self.livekit_worker_boot_status,
            "livekit_ready_source": self.livekit_ready_source,
            "livekit_ready": self.livekit_ready,
            "livekit_url": self.livekit_url,
            "voice_mode": self.voice_mode,
            "restart_required_for_voice": self.restart_required_for_voice,
            "livekit_message": self.livekit_message,
            "event": self.event,
            "message": self.message,
            "error": self.error,
            "failure_reason": self.failure_reason,
            "updated_at": self.updated_at,
            "session_failed": bool(self.failure_reason),
        }


_runtime_readiness = RuntimeReadinessState()
_agent_telemetry: dict[str, object] = {}
_last_ready_state_signature: tuple[object, ...] | None = None
_last_ready_debug_signature: tuple[object, ...] | None = None
_last_ready_debug_log_at: float = 0.0
_session_debug_lock = threading.RLock()
LIVEKIT_TOKEN_TIMEOUT_SECONDS = max(float(os.getenv("KIRA_LIVEKIT_TOKEN_TIMEOUT_SECONDS", "8.0") or "8.0"), 0.5)
SELFHOST_DISPATCH_TIMEOUT_SECONDS = max(float(os.getenv("KIRA_SELFHOST_DISPATCH_TIMEOUT_SECONDS", "8.0") or "8.0"), 0.5)
KIRA_BOOT_MODE = str(os.getenv("KIRA_BOOT_MODE", "minimal") or "minimal").strip().lower()
KIRA_LAZY_LOAD_EXTRAS = str(os.getenv("KIRA_LAZY_LOAD_EXTRAS", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
KIRA_BOOT_MINIMAL = KIRA_BOOT_MODE == "minimal"
KIRA_MEMORY_REQUIRED = str(os.getenv("KIRA_MEMORY_REQUIRED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
KIRA_START_OPTIONAL_SERVICES = str(os.getenv("KIRA_START_OPTIONAL_SERVICES", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
KIRA_PREPARE_SESSION_BEFORE_BUTTON = str(os.getenv("KIRA_PREPARE_SESSION_BEFORE_BUTTON", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
# Janela de frescor do token preparado: agora casa com o TTL do JWT (6h), pra
# que o usuario possa clicar INICIAR SESSAO a qualquer momento sem o token
# preparado ser considerado "velho". Quem realmente expira passa a ser o JWT
# (checado por token_expires_at em _prepared_token_is_valid). Default 6h (21600s).
# 4 min: o LiveKit Cloud fecha a sala sem gente em ~5 min e o agente sai junto; sala preparada mais velha que isso
# deixava a sessão "morta" no clique (2026-11-07: preparada 08:48, fechada 08:53, clique 10:26). Velha = sala nova no clique.
KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS = max(int(os.getenv("KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS", "240") or "240"), 10)
logger.info("LIVEKIT_TOKEN_TIMEOUT_CONFIG timeout_s=%.1f", LIVEKIT_TOKEN_TIMEOUT_SECONDS)
logger.info("LIVEKIT_DISPATCH_TIMEOUT_CONFIG timeout_s=%.1f", SELFHOST_DISPATCH_TIMEOUT_SECONDS)
logger.info(
    "KIRA_BOOT_MODE_CONFIG mode=%s lazy_load_extras=%s memory_required=%s start_optional_services=%s prepare_session_before_button=%s prepared_token_max_age_s=%s",
    KIRA_BOOT_MODE,
    str(KIRA_LAZY_LOAD_EXTRAS).lower(),
    str(KIRA_MEMORY_REQUIRED).lower(),
    str(KIRA_START_OPTIONAL_SERVICES).lower(),
    str(KIRA_PREPARE_SESSION_BEFORE_BUTTON).lower(),
    KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS,
)
logger.info("BOOT_PROFILE_SELECTED profile=%s", KIRA_BOOT_MODE)
if KIRA_BOOT_MINIMAL:
    logger.info("BOOT_MINIMAL_BEGIN")
    logger.info("MEMORY_NOT_REQUIRED_FOR_BOOT")
    for _feature_name in ("manus", "whatsapp", "browser", "vision", "music", "system_vitals"):
        logger.info("EXTRA_BOOT_SKIPPED feature=%s reason=boot_minimal", _feature_name)
    logger.debug("PC_OPERATOR_REMOVED_BOOT_SKIPPED")  # [REMOVED] PC_OPERATOR
_session_debug_state: dict[str, object] = {
    "requested_room_name": "",
    "room_name": "",
    "participant_name": "",
    "token_issued": False,
    "ui_room_connected": False,
    "ui_room_connected_at": "",
    "ui_mic_published": False,
    "ui_participant_identity": "",
    "ui_audio_track_sid": "",
    "ui_mic_health": {},
    "selfhost_dispatch_mode": "unknown",
    "dispatch_info": None,
    "room_prepared": False,
    "token_ready": False,
    "token_created_at": 0.0,
    "token_expires_at": 0.0,
    "last_event": "",
    "last_message": "",
    "last_error": "",
    "last_stage": "idle",
}
_prepared_session_lock = threading.RLock()
_prepared_session: dict[str, object] | None = None
_prepared_session_async_lock = asyncio.Lock()
_dispatch_locks: dict[str, asyncio.Lock] = {}
_dispatch_locks_guard = threading.Lock()
_dispatching_rooms: set[str] = set()
_dispatching_rooms_lock = threading.Lock()
_dispatch_refresh_attempts: dict[str, int] = {}
_dispatch_refresh_lock = threading.Lock()


def _room_dispatch_lock(room_name: str) -> asyncio.Lock:
    key = str(room_name or "").strip().lower()
    with _dispatch_locks_guard:
        lock = _dispatch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _dispatch_locks[key] = lock
        return lock


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


_READY_LOG_INTERVAL_SECONDS = 30.0
_READY_LOG_EVENTS = {
    "FUNCTIONAL_READY",
    "FUNCTIONAL_READY_PUBLISHED",
    "SESSION_RUNTIME_READY",
    "SESSION_RUNTIME_READY_PUBLISHED",
    "UI_STATUS_UPDATE",
}
_ready_log_state: dict[str, tuple[str, float, int]] = {}
_ready_republish_guard: dict[str, dict[str, object]] = {
    "SESSION_RUNTIME_READY": {"key": "", "skipped": 0, "last_summary_at": 0.0},
    "FUNCTIONAL_READY": {"key": "", "skipped": 0, "last_summary_at": 0.0},
}
_READY_REPUBLISH_SUMMARY_SECONDS = 30.0


def _runtime_log(event_name: str, **details) -> None:
    payload = " ".join(f"{k}={v}" for k, v in details.items() if v is not None and v != "")
    if event_name in _READY_LOG_EVENTS:
        now = time.monotonic()
        previous_signature, last_emitted, skipped = _ready_log_state.get(event_name, ("", 0.0, 0))
        if payload == previous_signature and now - last_emitted < _READY_LOG_INTERVAL_SECONDS:
            _ready_log_state[event_name] = (previous_signature, last_emitted, skipped + 1)
            return
        if skipped:
            print(f"READY_LOG_RATE_LIMITED event={event_name} skipped={skipped}", flush=True)
        _ready_log_state[event_name] = (payload, now, 0)
    print(f"{event_name} {payload}".strip(), flush=True)


def _ready_info_log(event_name: str) -> None:
    _runtime_log(event_name)


def _ready_publish_key_unlocked() -> str:
    room_name = str(_runtime_readiness.room_name or _runtime_readiness.requested_room_name or "").strip()
    boot_started_at = float(getattr(_runtime_readiness, "boot_started_at", 0.0) or 0.0)
    return f"room={room_name or 'none'}|boot={boot_started_at:.3f}"


def _ready_republish_already_published_unlocked(event_name: str, publish_key: str) -> bool:
    item = _ready_republish_guard.get(event_name) or {}
    return bool(publish_key and str(item.get("key") or "") == publish_key)


def _ready_republish_mark_published_unlocked(event_name: str, publish_key: str) -> None:
    item = _ready_republish_guard.setdefault(event_name, {})
    item["key"] = publish_key
    item["skipped"] = 0
    item["last_summary_at"] = 0.0


def _ready_republish_record_skip(event_name: str, publish_key: str) -> None:
    summary: tuple[str, int, str] | None = None
    now = time.monotonic()
    with _runtime_lock:
        item = _ready_republish_guard.setdefault(event_name, {})
        skipped = int(item.get("skipped") or 0) + 1
        item["skipped"] = skipped
        last_summary_at = float(item.get("last_summary_at") or 0.0)
        if now - last_summary_at >= _READY_REPUBLISH_SUMMARY_SECONDS:
            item["last_summary_at"] = now
            summary = (event_name, skipped, publish_key)
    if summary:
        _runtime_log("READY_REPUBLISH_SKIPPED", event=summary[0], skipped=summary[1], key=summary[2])


def _reset_ready_republish_guard(reason: str) -> None:
    old_keys: dict[str, str] = {}
    with _runtime_lock:
        for event_name, item in _ready_republish_guard.items():
            old_keys[event_name] = str(item.get("key") or "")
            item["key"] = ""
            item["skipped"] = 0
            item["last_summary_at"] = 0.0
    old_key = ",".join(f"{event}={key or 'none'}" for event, key in old_keys.items())
    _runtime_log("READY_REPUBLISH_GUARD_RESET", reason=reason, old_key=old_key)


class SessionStartStageError(RuntimeError):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        status_code: int = 502,
        error_code: str = "session_start_failed",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.error_code = error_code
        self.details = details or {}


class SessionStartStageTimeout(SessionStartStageError):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        timeout_s: float,
        elapsed_ms: int,
        details: dict[str, object] | None = None,
    ) -> None:
        payload = {"timeout_s": timeout_s, "elapsed_ms": elapsed_ms, **(details or {})}
        super().__init__(
            stage,
            message,
            status_code=504,
            error_code=f"{stage}_timeout",
            details=payload,
        )
        self.timeout_s = timeout_s
        self.elapsed_ms = elapsed_ms


def _elapsed_ms(start: float) -> int:
    return int(max((time.perf_counter() - start) * 1000, 0))


def _session_start_log(event_name: str, *, level: str = "info", **details) -> None:
    payload = " ".join(f"{key}={value}" for key, value in details.items() if value is not None and value != "")
    getattr(logger, level, logger.info)("%s%s%s", event_name, " " if payload else "", payload)
    _runtime_log(event_name, **details)


def _livekit_failed_slots_snapshot() -> set[int]:
    with _livekit_failed_slots_lock:
        return set(_livekit_boot_failed_slots)


def _clear_livekit_failed_slots() -> None:
    with _livekit_failed_slots_lock:
        _livekit_boot_failed_slots.clear()


def _mark_livekit_failed_slot(slot: int | None, *, reason: str) -> None:
    try:
        target_slot = int(slot) if slot is not None else None
    except Exception:
        target_slot = None
    if target_slot is None:
        return
    with _livekit_failed_slots_lock:
        _livekit_boot_failed_slots.add(target_slot)
    if reason == "quota_exceeded":
        LIVEKIT_MANAGER.mark_quota_exceeded(target_slot)
    elif reason == "auth_failed":
        LIVEKIT_MANAGER.mark_auth_failed(target_slot)


def _ordered_livekit_credentials(*, exclude_failed_slots: bool = True) -> list[LiveKitCredential]:
    exclude_slots = _livekit_failed_slots_snapshot() if exclude_failed_slots else set()
    return LIVEKIT_MANAGER.ordered_credentials(exclude_slots=exclude_slots)


def _select_livekit_worker_credential() -> LiveKitCredential | None:
    ordered = _ordered_livekit_credentials(exclude_failed_slots=True)
    if not ordered:
        return None
    return ordered[0]


def _current_livekit_credential_for_session() -> LiveKitCredential | None:
    current_slot = LIVEKIT_MANAGER.current_slot
    for credential in _ordered_livekit_credentials(exclude_failed_slots=False):
        if current_slot is not None and credential.slot == current_slot:
            return credential
    return _select_livekit_worker_credential()


def _resolve_livekit_credential_for_room(room_name: str | None = None) -> LiveKitCredential | None:
    credential = _current_livekit_credential_for_session()
    if credential is not None:
        return credential
    return _select_livekit_worker_credential()


def _apply_livekit_credential_to_env(env: dict[str, str], credential: LiveKitCredential) -> None:
    provider = _selected_livekit_provider()
    slot_value = str(credential.slot)
    env["LIVEKIT_ACTIVE_SLOT"] = slot_value
    env["LIVEKIT_URL"] = credential.url
    env["LIVEKIT_API_KEY"] = credential.api_key
    env["LIVEKIT_API_SECRET"] = credential.api_secret
    env["LIVEKIT_PROVIDER_SELECTED"] = provider.name
    env["LIVEKIT_PROVIDER_KIND"] = provider.kind
    os.environ["LIVEKIT_ACTIVE_SLOT"] = slot_value
    os.environ["LIVEKIT_URL"] = credential.url
    os.environ["LIVEKIT_API_KEY"] = credential.api_key
    os.environ["LIVEKIT_API_SECRET"] = credential.api_secret
    os.environ["LIVEKIT_PROVIDER_SELECTED"] = provider.name
    os.environ["LIVEKIT_PROVIDER_KIND"] = provider.kind
    LIVEKIT_MANAGER.mark_current(credential.slot)
    logger.info("LIVEKIT_AGENT_PROVIDER_ENV_SET name=%s kind=%s url=%s", provider.name, provider.kind, provider.url)


def _runtime_state_snapshot() -> dict:
    with _runtime_lock:
        state = _runtime_readiness.as_dict()
    return _augment_runtime_state(state)


def _update_session_debug(**changes) -> dict[str, object]:
    with _session_debug_lock:
        for key, value in changes.items():
            _session_debug_state[key] = value
        return dict(_session_debug_state)


def _reset_session_debug_for_new_attempt(*, room_name: str = "", participant_name: str = "") -> dict[str, object]:
    with _dispatch_refresh_lock:
        _dispatch_refresh_attempts.clear()
    _reset_ready_republish_guard("new_session_attempt")
    return _update_session_debug(
        requested_room_name=str(room_name or "").strip(),
        room_name="",
        participant_name=str(participant_name or "").strip(),
        token_issued=False,
        ui_room_connected=False,
        ui_room_connected_at="",
        selfhost_dispatch_mode="pending",
        dispatch_info=None,
        room_prepared=False,
        token_ready=False,
        ui_mic_health={},
        token_created_at=0.0,
        token_expires_at=0.0,
        last_event="TOKEN_REQUEST_RECEIVED",
        last_message="Token solicitado. Preparando sessao...",
        last_error="",
        last_stage="token_requested",
    )


def _prepared_token_is_valid(prepared: dict[str, object] | None = None) -> bool:
    candidate = prepared
    if candidate is None:
        with _prepared_session_lock:
            candidate = dict(_prepared_session or {})
    if not candidate:
        return False
    token = str(candidate.get("token") or candidate.get("participantToken") or "").strip()
    created_at = float(candidate.get("token_created_at") or 0.0)
    expires_at = float(candidate.get("token_expires_at") or 0.0)
    if not token or created_at <= 0:
        return False
    now = time.time()
    if expires_at and now >= (expires_at - 5):
        return False
    return (now - created_at) < KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS


def _get_prepared_session() -> dict[str, object] | None:
    with _prepared_session_lock:
        if _prepared_token_is_valid(_prepared_session):
            return dict(_prepared_session or {})
    return None


def _save_prepared_session(payload: dict[str, object]) -> dict[str, object]:
    global _prepared_session
    token_created_at = float(payload.get("token_created_at") or time.time())
    timing_id = str(payload.get("session_timing_id") or "").strip() or record_timing(
        event="session_prepare_token_ready",
        source="backend",
        room=str(payload.get("room_name") or payload.get("roomName") or ""),
    )
    prepared = {
        **payload,
        "room_prepared": True,
        "roomPrepared": True,
        "token_ready": True,
        "tokenReady": True,
        "token_created_at": token_created_at,
        "token_expires_at": float(payload.get("token_expires_at") or (token_created_at + KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS)),
        "session_timing_id": timing_id,
    }
    with _prepared_session_lock:
        _prepared_session = dict(prepared)
    _update_session_debug(
        requested_room_name=str(prepared.get("room_name") or prepared.get("roomName") or ""),
        room_name=str(prepared.get("room_name") or prepared.get("roomName") or ""),
        participant_name=str(prepared.get("participant_name") or prepared.get("participantName") or ""),
        token_issued=True,
        room_prepared=True,
        token_ready=True,
        token_created_at=float(prepared.get("token_created_at") or 0.0),
        token_expires_at=float(prepared.get("token_expires_at") or 0.0),
        dispatch_info=(
            dict(prepared.get("dispatchInfo") or prepared.get("agentDispatch"))
            if isinstance(prepared.get("dispatchInfo") or prepared.get("agentDispatch"), dict)
            else None
        ),
        selfhost_dispatch_mode=(
            str(prepared.get("selfhostDispatchMode") or "explicit_dispatch")
            if isinstance(prepared.get("dispatchInfo") or prepared.get("agentDispatch"), dict)
            else "prepared_no_dispatch"
        ),
        last_event="SESSION_PREPARE_READY_FOR_BUTTON",
        last_message="Room/token preparados. Aguardando clique do usuario.",
        last_error="",
        last_stage="session_prepared",
    )
    return prepared


def _prepared_session_to_response(prepared: dict[str, object]) -> dict[str, object]:
    dispatch_info = prepared.get("dispatchInfo") or prepared.get("agentDispatch")
    normalized_dispatch = dict(dispatch_info) if isinstance(dispatch_info, dict) else None
    return {
        **prepared,
        "ok": True,
        "room_prepared": True,
        "roomPrepared": True,
        "token_ready": True,
        "tokenReady": True,
        "can_show_start_button": True,
        "can_start_session": True,
        "show_start_button": True,
        "dispatchInfo": normalized_dispatch,
        "agentDispatch": normalized_dispatch,
        "selfhostDispatchMode": str(
            prepared.get("selfhostDispatchMode")
            or ("explicit_dispatch" if normalized_dispatch else "prepared_no_dispatch")
        ),
    }


def _session_debug_snapshot() -> dict[str, object]:
    with _session_debug_lock:
        debug_state = dict(_session_debug_state)
    state = _runtime_state_snapshot()
    _room_name_for_dispatch = debug_state.get("room_name") or state.get("room_name") or ""
    with _dispatch_refresh_lock:
        _dispatch_attempts_now = _dispatch_refresh_attempts.get(str(_room_name_for_dispatch), 0)
    _entrypoint_called = bool(state.get("agent_connected") or state.get("agent_ready") or state.get("room_ready"))
    _dispatch_failed = _dispatch_attempts_now > 0 and not _entrypoint_called
    if _dispatch_failed:
        logger.info(
            "SESSION_DEBUG_DISPATCH_STATE room=%s entrypoint_called=false last_dispatch_attempts=%s",
            _room_name_for_dispatch or "none",
            _dispatch_attempts_now,
        )
    debug_state.update(
        {
            "requested_room_name": debug_state.get("requested_room_name") or state.get("requested_room_name") or "",
            "room_name": _room_name_for_dispatch,
            # sala preparada na abertura (antes do clique): o agente aceita o job dela em vez de recusar como "velha"
            "prepared_room_name": str((_get_prepared_session() or {}).get("room_name") or ""),
            "agent_process_spawned": bool(state.get("agent_process_spawned")),
            "agent_connected": bool(state.get("agent_connected")),
            "agent_ready": bool(state.get("agent_ready")),
            "room_connected": bool(state.get("room_connected")),
            "room_ready": bool(state.get("room_ready")),
            "gemini_ready": bool(state.get("gemini_ready")),
            "voice_pipeline_ready": bool(state.get("voice_pipeline_ready")),
            "runtime_ready": bool(state.get("runtime_ready")),
            "functional_ready": bool(state.get("functional_ready")),
            "livekit_status": state.get("livekit_status"),
            "voice_mode": state.get("voice_mode"),
            "backend_ready": bool(state.get("backend_ready")),
            "token_status": state.get("token_status"),
            "pre_start_ready": bool(state.get("pre_start_ready")),
            "last_event": debug_state.get("last_event") or state.get("event") or "",
            "last_message": debug_state.get("last_message") or state.get("message") or "",
            "last_error": debug_state.get("last_error") or state.get("error") or state.get("failure_reason") or "",
            "last_stage": debug_state.get("last_stage") or state.get("boot_state") or "",
            "entrypoint_called": _entrypoint_called,
            "last_dispatch_attempts": _dispatch_attempts_now,
            "last_dispatch_error": "worker_unavailable" if _dispatch_failed else None,
            "last_dispatch_error_code": "no_response_from_servers" if _dispatch_failed else None,
            "ui_mic_published": bool(debug_state.get("ui_mic_published")),
            "ui_participant_identity": debug_state.get("ui_participant_identity") or "",
            "ui_audio_track_sid": debug_state.get("ui_audio_track_sid") or "",
        }
    )
    return debug_state


def _session_debug_flags() -> tuple[bool, bool, bool, str]:
    with _session_debug_lock:
        token_issued = bool(_session_debug_state.get("token_issued"))
        token_ready = bool(_session_debug_state.get("token_ready"))
        room_prepared = bool(_session_debug_state.get("room_prepared"))
        last_stage = str(_session_debug_state.get("last_stage") or "").strip().lower()
    prepared_valid = _prepared_token_is_valid()
    return bool(token_issued or prepared_valid), bool(token_ready or prepared_valid), bool(room_prepared or prepared_valid), last_stage


def _format_livekit_key(slot: int | None, total_slots: int | None = None) -> str:
    total = int(total_slots or LIVEKIT_MANAGER.total_slots or 4)
    return f"{slot if slot is not None else '-'}/{total}"


def _livekit_status_label(status: str) -> str:
    mapping = {
        "ok": "OK",
        "unknown": "Aguardando",
        "starting": "Inicializando",
        "auth_failed": "credenciais invalidas",
        "quota_exceeded": "sem cota",
        "all_failed": "todas falharam",
        "error": "erro",
    }
    normalized = str(status or "unknown").strip().lower() or "unknown"
    return mapping.get(normalized, "desconhecido")


def _gemini_boot_status() -> str:
    if bool(_runtime_readiness.gemini_ready):
        return "ok"
    report = _load_agent_key_report()
    if int(report.get("available_text_keys", 0) or 0) > 0:
        return "ok"
    if int(report.get("usable_keys", 0) or 0) > 0:
        return "degraded"
    return "unavailable"


def _augment_runtime_state(state: dict | None) -> dict:
    global _last_ready_state_signature
    data = dict(state or {})
    livekit_status = str(data.get("livekit_status") or "unknown").strip().lower() or "unknown"
    data["livekit_server_ok"] = bool(data.get("livekit_server_ok")) or livekit_status in {"ready", "ok", "connected", "local_ready"}
    gemini_status = str(data.get("gemini_status") or "").strip().lower() or _gemini_boot_status()
    backend_ready = bool(data.get("backend_ready"))
    if not backend_ready and (_server_ready.is_set() or bool(data.get("app_started"))):
        backend_ready = True
        data["backend_ready"] = True
        data["backend_ready_source"] = "http_server_started" if _server_ready.is_set() else "endpoint_self_alive"
    data.setdefault("backend_status", "ready" if backend_ready else "starting")
    data.setdefault("backend_url", f"http://127.0.0.1:{PORT}")
    agent_process_spawned = bool(data.get("agent_process_spawned"))
    agent_process_alive = bool(agent_process_spawned and not bool(data.get("agent_process_dead")))
    agent_error = bool(data.get("agent_error") or (agent_process_spawned and bool(data.get("agent_process_dead"))))
    agent_last_error = str(data.get("agent_last_error") or data.get("error") or data.get("failure_reason") or "").strip()
    agent_connected = bool(data.get("agent_connected"))
    agent_worker_ready = bool(
        data.get("agent_worker_ready")
        or data.get("worker_ready_received")
        or data.get("agent_ready_phase") == "worker_ready"
        or data.get("agent_probe_ok")
        or data.get("agent_ready")
    )

    debug_token_issued, debug_token_ready, debug_room_prepared, debug_last_stage = _session_debug_flags()

    token_status = "pending"
    if debug_token_issued and debug_token_ready:
        token_status = "ok"
    elif debug_last_stage == "token_failed" or livekit_status in {"all_failed", "auth_failed", "quota_exceeded", "dns_failed", "cert_failed", "connect_failed", "provider_failed", "disconnected", "reconnecting", "error"}:
        token_status = "failed"

    room_status = "not_ready"
    if bool(data.get("room_connecting")):
        room_status = "connecting"
    elif bool(data.get("room_ready")) or bool(data.get("session_started")):
        room_status = "ready"

    heartbeat_seen = float(data.get("last_agent_heartbeat_at") or 0.0) > 0
    heartbeat_status = str(data.get("agent_heartbeat_status") or ("ok" if data.get("agent_heartbeat_ok") else "pending")).strip().lower()
    heartbeat_transient = bool(heartbeat_status == "stale" and agent_process_alive and not agent_error)
    heartbeat_ok = bool(data.get("agent_heartbeat_ok")) or not heartbeat_seen or heartbeat_transient
    agent_ok = bool(agent_process_alive and agent_worker_ready and not agent_error and heartbeat_ok)
    livekit_ready_states = {"ready", "ok", "connected", "local_ready"}
    livekit_server_ok = bool(data.get("livekit_server_ok")) or livekit_status in {"ready", "ok", "connected", "local_ready"}
    if livekit_status == "starting" and livekit_server_ok and agent_ok:
        logger.info("[LIVEKIT_STATUS_DEFENSIVE_FIX] old=starting new=ready reason=server_ok_agent_ok")
        livekit_status = "ready"
        data["livekit_status"] = "ready"
        data["livekit_ready_source"] = "server_ok_agent_ok_defensive_fix"
        _update_livekit_runtime_state(
            livekit_status="ready",
            livekit_ready_source="server_ok_agent_ok_defensive_fix",
            livekit_message=_livekit_runtime_message_for_status("ready"),
            log_reason="server_ok_agent_ok_defensive_fix",
        )
    livekit_ready = livekit_status in livekit_ready_states
    memory_ready = bool(data.get("memory_ready"))
    memory_status = str(data.get("memory_status") or "unknown").strip().lower() or "unknown"
    memory_error = str(data.get("memory_error") or "").strip()
    livekit_blocking = livekit_status in {
        "all_failed",
        "auth_failed",
        "quota_exceeded",
        "dns_failed",
        "cert_failed",
        "connect_failed",
        "provider_failed",
        "disconnected",
        "error",
    }
    gemini_blocking = gemini_status == "unavailable"

    pre_start_ready = bool(
        backend_ready
        and agent_ok
        and livekit_ready
        and token_status != "failed"
    )
    degraded_start_ready = bool(
        backend_ready
        and agent_ok
        and not livekit_blocking
    )
    core_ready = bool(backend_ready and agent_ok and livekit_ready and not livekit_blocking)
    prepared_required = False
    prepared_ready = True
    can_start_session = bool(core_ready and token_status != "failed")
    show_start_button = can_start_session

    ready_debug_signature = (backend_ready, agent_ok, livekit_status, token_status, debug_room_prepared, debug_token_ready, pre_start_ready, can_start_session)
    global _last_ready_debug_signature, _last_ready_debug_log_at
    now_mono = time.monotonic()
    if ready_debug_signature != _last_ready_debug_signature or (now_mono - _last_ready_debug_log_at) >= 5.0:
        _last_ready_debug_signature = ready_debug_signature
        _last_ready_debug_log_at = now_mono
        logger.info(
            "[READY_DEBUG] backend=%s agent_ok=%s livekit=%s token=%s room_prepared=%s token_ready=%s | lk_block=%s | pre_start=%s degraded=%s can_start=%s",
            backend_ready,
            agent_ok,
            livekit_status,
            token_status,
            debug_room_prepared,
            debug_token_ready,
            livekit_blocking,
            pre_start_ready,
            degraded_start_ready,
            can_start_session,
        )
    else:
        logger.debug("[READY_DEBUG_THROTTLED]")

    room_requested = bool(
        str(data.get("requested_room_name") or data.get("room_name") or "").strip()
    )
    session_functional_ready = bool(
        room_requested
        and backend_ready
        and livekit_ready
        and agent_ok
        and data.get("room_ready")
        and data.get("gemini_ready")
        and data.get("voice_pipeline_ready")
        and data.get("runtime_ready")
        and data.get("functional_ready")
    )
    if bool(data.get("functional_ready")) and not session_functional_ready:
        reason = "agent_not_ok" if not agent_ok else "critical_runtime_signal_missing"
        logger.warning("READINESS_CONTRADICTION_FIXED field=functional_ready reason=%s", reason)
        data["functional_ready"] = False
    logger.debug(
        "READINESS_CONSISTENCY_CHECK effective_functional_ready=%s agent_ok=%s heartbeat_status=%s",
        str(session_functional_ready).lower(),
        str(agent_ok).lower(),
        heartbeat_status,
    )

    missing_flags: list[str] = []
    if room_requested and not session_functional_ready:
        if not debug_token_issued:
            missing_flags.append("token_issued")
        if not debug_token_ready:
            missing_flags.append("token_ready")
        if not debug_room_prepared:
            missing_flags.append("room_prepared")
        if not bool(data.get("room_ready")):
            missing_flags.append("room_ready")
        if not bool(data.get("gemini_ready")):
            missing_flags.append("gemini_ready")
        if not bool(data.get("voice_pipeline_ready")):
            missing_flags.append("voice_pipeline_ready")
        if not bool(data.get("runtime_ready")):
            missing_flags.append("runtime_ready")
    elif not backend_ready:
        missing_flags.append("backend_ready")
    if not livekit_ready:
        missing_flags.append("livekit_status")
    if not agent_ok:
        if not agent_process_alive:
            missing_flags.append("agent_process_spawned")
        if not agent_worker_ready:
            missing_flags.append("agent_worker_ready")
    if token_status == "failed":
        missing_flags.append("token_status")

    diagnostic_flags: list[str] = []
    if room_status != "ready":
        diagnostic_flags.append("room_status")
    if not bool(data.get("session_started")):
        diagnostic_flags.append("session_not_started")

    block_reason = ""
    runtime_failed_before_ready = bool(
        room_requested
        and agent_error
        and (bool(data.get("room_connected")) or bool(data.get("gemini_ready")))
        and (not bool(data.get("voice_pipeline_ready")))
        and (not bool(data.get("runtime_ready")))
        and (not bool(data.get("functional_ready")))
    )

    if runtime_failed_before_ready:
        failure_detail = agent_last_error or "agent_process_dead_before_functional_ready"
        data["session_failed"] = True
        data["runtime_status"] = "failed"
        data["runtime_failed"] = True
        data["agent_ok"] = False
        data["agent_process_alive"] = False
        data["voice_pipeline_ready"] = False
        data["runtime_ready"] = False
        data["functional_ready"] = False
        data["can_start_session"] = False
        logger.error("AGENT_RUNTIME_FAILED error=%s", failure_detail)
        logger.error("READY_STATE_RUNTIME_FAILED error=%s", failure_detail)
        logger.error("SESSION_RUNTIME_ABORTED reason=agent_process_died_before_functional_ready")
        block_reason = f"Agent falhou durante runtime: {failure_detail}"
    elif not can_start_session and not session_functional_ready:
        if agent_error:
            block_reason = "Agent falhou ao iniciar"
            logger.error("[READY_STATE_AGENT_FAILED] error=%s", agent_last_error or "agent_process_dead")
        elif room_requested:
            pretty_missing = ", ".join(missing_flags) if missing_flags else "runtime_ready"
            block_reason = f"Sessao iniciada. Aguardando runtime: {pretty_missing}"
        elif not backend_ready or not agent_ok or not livekit_ready or livekit_blocking or token_status == "failed":
            faltando: list[str] = []
            if not backend_ready:
                faltando.append("backend")
            if livekit_blocking or not livekit_ready:
                faltando.append("livekit")
            if not agent_ok:
                faltando.append("agent_worker")
            if token_status == "failed":
                faltando.append("token")
            block_reason = f"Aguardando: {', '.join(faltando) if faltando else 'inicializacao'}"
        elif prepared_required and (not debug_room_prepared or not debug_token_ready):
            faltando_preparo = []
            if not debug_room_prepared:
                faltando_preparo.append("room_prepared")
            if not debug_token_ready:
                faltando_preparo.append("token_ready")
            block_reason = f"Preparando sessao: {', '.join(faltando_preparo)}"
        elif agent_process_alive and not agent_worker_ready:
            block_reason = "Aguardando worker do agente"
        elif (not agent_process_alive) and (not agent_worker_ready):
            block_reason = "Agent caiu antes de registrar"
        else:
            block_reason = "Finalizando inicializacao..."
    elif room_status != "ready":
        block_reason = "Sistema pronto. Clique para iniciar sessao."

    ready_signature = (
        pre_start_ready,
        can_start_session,
        room_status,
        token_status,
        block_reason,
        tuple(missing_flags),
    )
    if ready_signature != _last_ready_state_signature:
        _last_ready_state_signature = ready_signature
        _runtime_log(
            "READY_STATE",
            pre_start_ready=str(pre_start_ready).lower(),
            can_start_session=str(can_start_session).lower(),
            room_status=room_status,
            token_status=token_status,
            block_reason=block_reason or None,
            missing_flags=",".join(missing_flags) if missing_flags else None,
            room_status_ignored_before_click="true" if room_status != "ready" else "false",
        )
        logger.info(
            "[KIRA READY STATE] status pre_start_ready=%s can_start_session=%s backend_ready=%s livekit_status=%s room_prepared=%s token_ready=%s block_reason=%s missing_flags=%s",
            str(pre_start_ready).lower(),
            str(can_start_session).lower(),
            str(backend_ready).lower(),
            livekit_status,
            str(debug_room_prepared).lower(),
            str(debug_token_ready).lower(),
            block_reason or "<none>",
            ",".join(missing_flags) if missing_flags else "<none>",
        )
        logger.info(
            "[KIRA_READY_STATE] agent_process_alive=%s agent_worker_ready=%s agent_ok=%s",
            str(agent_process_alive).lower(),
            str(agent_worker_ready).lower(),
            str(agent_ok).lower(),
        )

    data["gemini_status"] = gemini_status
    data["token_status"] = token_status
    data["token_issued"] = debug_token_issued
    data["token_ready"] = debug_token_ready
    data["room_prepared"] = debug_room_prepared
    data["core_ready"] = core_ready
    data["room_status"] = room_status
    data["pre_start_ready"] = pre_start_ready
    data["degraded_start_ready"] = degraded_start_ready
    data["can_start_session"] = can_start_session
    data["show_start_button"] = show_start_button
    data["agent_process_alive"] = agent_process_alive
    data["agent_worker_ready"] = agent_worker_ready
    data["agent_ok"] = agent_ok
    data["agent_heartbeat_status"] = heartbeat_status
    data["agent_error"] = agent_error
    data["agent_last_error"] = agent_last_error
    data["voice_ready"] = bool(session_functional_ready)
    data["text_ready"] = bool(
        data.get("voice_mode") == "text_only"
        or session_functional_ready
        or (backend_ready and agent_ok)
        or (
            backend_ready
            and (
                livekit_blocking
                or bool(data.get("boot_recovery_active"))
                or bool(data.get("restart_required_for_voice"))
                or bool(agent_error)
            )
        )
    )
    data["degraded"] = bool(data["text_ready"] and not data["voice_ready"])
    provider = get_selected_provider()
    data["livekit_provider_active"] = str(getattr(provider, "name", "") or "unknown")
    data["livekit_provider_type"] = str(getattr(provider, "kind", "") or "unknown")
    data["livekit_provider_health"] = "degraded" if data["degraded"] else ("ready" if livekit_ready else livekit_status)
    data["memory_ready"] = memory_ready
    data["memory_status"] = memory_status
    data["memory_error"] = memory_error
    data["missing_flags"] = missing_flags
    data["diagnostic_flags"] = diagnostic_flags
    data["block_reason"] = "" if session_functional_ready else block_reason
    return data


def _sync_runtime_status_manager_from_legacy(
    state: dict | None = None,
    *,
    already_augmented: bool = False,
) -> dict[str, object]:
    """Reconcile the canonical snapshot with the legacy readiness producer.

    A parte antiga do boot ainda alimenta ``_runtime_readiness`` enquanto
    ``RuntimeStatusManager`` é a API oficial. Sincronizar todos os sinais, em
    vez de apenas os campos alterados no último evento, evita que
    ``/api/runtime-status`` fique BOOTING/sem backend enquanto
    ``/api/system-status`` já informa a sessão pronta.
    """
    if state is None:
        effective = _runtime_state_snapshot()
    elif already_augmented:
        effective = dict(state)
    else:
        effective = _augment_runtime_state(dict(state))

    livekit_status = str(effective.get("livekit_status") or "unknown").strip().lower()
    livekit_ready = livekit_status in {"ready", "ok", "connected", "local_ready"}
    process_dead = bool(effective.get("agent_process_dead"))
    worker_ready = bool(effective.get("agent_worker_ready")) and not process_dead
    agent_alive = bool(effective.get("agent_process_alive")) and not process_dead
    failed = bool(effective.get("fatal_error"))
    error_message = str(effective.get("error") or effective.get("failure_reason") or "").strip() or None
    manager = get_runtime_status_manager()
    try:
        manager._update(
            backend_ready=bool(effective.get("backend_ready")),
            livekit_ready=livekit_ready,
            worker_ready=worker_ready,
            memory_ready=bool(effective.get("memory_ready")),
            memory_status=str(effective.get("memory_status") or "unknown"),
            memory_error=str(effective.get("memory_error") or "") or None,
            room_ready=bool(effective.get("room_ready") or effective.get("room_connected")),
            gemini_ready=bool(effective.get("gemini_ready")),
            voice_pipeline_ready=bool(effective.get("voice_pipeline_ready")),
            runtime_ready=bool(effective.get("runtime_ready")),
            functional_ready=bool(effective.get("functional_ready")),
            recovery_active=bool(effective.get("boot_recovery_active")),
            agent_process_alive=agent_alive,
            fatal_error=failed,
            error_message=error_message if failed else None,
        )
    except Exception as exc:
        logger.debug("RUNTIME_STATUS_CANONICAL_SYNC_SKIPPED reason=%s", exc)
    return manager.get_snapshot()


def _sync_boot_orchestrator_from_runtime_state(
    state: dict | None = None,
    *,
    reason: str = "runtime_sync",
) -> dict[str, object]:
    snapshot = dict(state or _runtime_state_snapshot())
    livekit_status = str(snapshot.get("livekit_status") or "unknown").strip().lower() or "unknown"
    gemini_status = str(snapshot.get("gemini_status") or "").strip().lower() or "unknown"

    backend_http_ok = bool(_server_ready.is_set() or snapshot.get("backend_ready"))
    livekit_server_ok = bool(livekit_status == "ok")
    agent_process_spawned = bool(snapshot.get("agent_process_spawned"))
    agent_worker_ready = bool(
        snapshot.get("agent_worker_ready")
        or snapshot.get("worker_ready_received")
        or snapshot.get("agent_ready_phase") == "worker_ready"
        or snapshot.get("agent_probe_ok")
        or snapshot.get("agent_ready")
    )
    memory_ready = bool(snapshot.get("memory_ready"))
    gemini_available = bool(snapshot.get("gemini_ready")) or gemini_status == "ok"

    boot_orchestrator.set_flag("backend_http_ok", backend_http_ok)
    boot_orchestrator.set_flag("livekit_server_ok", livekit_server_ok)
    boot_orchestrator.set_flag("agent_process_spawned", agent_process_spawned)
    boot_orchestrator.set_flag("agent_worker_ready", agent_worker_ready)
    boot_orchestrator.set_flag("memory_ready", memory_ready)
    boot_orchestrator.set_flag("gemini_available", gemini_available)

    if str(snapshot.get("failure_reason") or snapshot.get("error") or "").strip() and str(snapshot.get("boot_state") or "").strip().lower() == "failed":
        boot_orchestrator.fail(str(snapshot.get("failure_reason") or snapshot.get("error") or "Falha no boot."))
        return boot_orchestrator.get_state()

    if bool(snapshot.get("functional_ready")) or bool(snapshot.get("runtime_ready")):
        boot_orchestrator.set_phase(BootPhase.SESSION_READY, details=reason)
    elif backend_http_ok and livekit_server_ok and agent_worker_ready:
        boot_orchestrator.set_phase(BootPhase.SYSTEM_READY, details=reason)
    elif agent_worker_ready:
        boot_orchestrator.set_phase(BootPhase.AGENT_READY, details=reason)
    elif agent_process_spawned:
        boot_orchestrator.set_phase(BootPhase.AGENT_STARTING, details=reason)
    elif livekit_server_ok:
        boot_orchestrator.set_phase(BootPhase.LIVEKIT_READY, details=reason)
    elif backend_http_ok:
        boot_orchestrator.set_phase(BootPhase.BACKEND_READY, details=reason)

    return boot_orchestrator.get_state()


def _build_boot_state_payload() -> dict[str, object]:
    legacy_state = _augment_runtime_state(_runtime_state_snapshot())
    debug = _session_debug_snapshot()
    boot_state = _sync_boot_orchestrator_from_runtime_state(legacy_state, reason="boot_state_payload")

    legacy_can_start = bool(
        legacy_state.get("can_start_session")
        or legacy_state.get("pre_start_ready")
    )
    boot_can_start = bool(boot_state.get("can_start_session"))
    final_memory_ready = bool(legacy_state.get("memory_ready") or boot_state.get("memory_ready"))
    final_memory_status = str(legacy_state.get("memory_status") or "unknown")
    final_memory_error = str(legacy_state.get("memory_error") or "")
    final_room_prepared = bool(legacy_state.get("room_prepared"))
    final_token_ready = bool(legacy_state.get("token_ready") or legacy_state.get("token_issued"))
    final_can_start = bool((legacy_can_start or boot_can_start) and final_room_prepared and final_token_ready)
    final_show_start_button = bool(
        final_can_start
        or legacy_state.get("show_start_button")
        or boot_state.get("show_start_button")
    )
    final_backend_ready = bool(
        legacy_state.get("backend_ready") or boot_state.get("backend_ready")
    )
    final_livekit_status = (
        legacy_state.get("livekit_status")
        or boot_state.get("livekit_status")
        or "starting"
    )
    final_livekit_ready = bool(
        legacy_state.get("livekit_ready")
        or str(final_livekit_status).strip().lower() in {"ready", "ok", "connected", "local_ready"}
    )
    final_agent_ok = bool(legacy_state.get("agent_ok"))
    final_block_reason = (
        "Sistema pronto. Clique para iniciar sessao."
        if final_can_start
        else (
            legacy_state.get("block_reason")
            or boot_state.get("block_reason")
            or legacy_state.get("livekit_message")
            or ""
        )
    )
    final_missing_flags = (
        []
        if final_can_start
        else (legacy_state.get("missing_flags") or boot_state.get("missing_flags") or [])
    )
    final_phase = (
        "ready"
        if final_can_start
        else (boot_state.get("phase") or legacy_state.get("phase") or "starting")
    )

    if legacy_can_start != boot_can_start:
        logger.warning(
            "[BOOT_STATE_CONTRACT_FIX] top_can_start=%s legacy_can_start=%s final_can_start=%s phase=%s block_reason=%s",
            boot_state.get("can_start_session"),
            legacy_state.get("can_start_session"),
            final_can_start,
            boot_state.get("phase"),
            legacy_state.get("block_reason"),
        )

    if final_can_start:
        logger.info(
            "[BOOT_STATE_PAYLOAD_READY] can_start_session=%s show_start_button=%s phase=%s",
            final_can_start,
            final_show_start_button,
            final_phase,
        )

    final_state = {
        **legacy_state,
        "backend_ready": final_backend_ready,
        "livekit_status": final_livekit_status,
        "livekit_ready": final_livekit_ready,
        "memory_ready": final_memory_ready,
        "memory_status": final_memory_status,
        "memory_error": final_memory_error,
        "agent_ok": final_agent_ok,
        "room_prepared": final_room_prepared,
        "token_ready": final_token_ready,
        "pre_start_ready": final_can_start,
        "can_start_session": final_can_start,
        "show_start_button": final_show_start_button,
        "block_reason": final_block_reason,
        "missing_flags": final_missing_flags,
        "optional_degraded_services": get_degraded_services(),
    }

    return {
        **boot_state,
        "ready": bool(final_can_start or legacy_state.get("functional_ready")),
        "functional_ready": bool(legacy_state.get("functional_ready")),
        "backend_ready": final_backend_ready,
        "livekit_status": final_livekit_status,
        "livekit_ready": final_livekit_ready,
        "memory_ready": final_memory_ready,
        "memory_status": final_memory_status,
        "memory_error": final_memory_error,
        "agent_connected": bool(
            legacy_state.get("agent_connected") or boot_state.get("agent_connected")
        ),
        "agent_ready": bool(
            legacy_state.get("agent_ready") or boot_state.get("agent_ready")
        ),
        "agent_ok": final_agent_ok,
        "room_prepared": final_room_prepared,
        "token_ready": final_token_ready,
        "token_created_at": legacy_state.get("token_created_at") or debug.get("token_created_at") or 0,
        "token_expires_at": legacy_state.get("token_expires_at") or debug.get("token_expires_at") or 0,
        "pre_start_ready": final_can_start,
        "can_start_session": final_can_start,
        "show_start_button": final_show_start_button,
        "phase": final_phase,
        "message": legacy_state.get("message") or "",
        "last_stage": str(debug.get("last_stage") or legacy_state.get("boot_state") or boot_state.get("phase") or ""),
        "last_event": str(debug.get("last_event") or legacy_state.get("event") or ""),
        "last_error": str(debug.get("last_error") or legacy_state.get("error") or legacy_state.get("failure_reason") or boot_state.get("error") or ""),
        "boot_error": legacy_state.get("failure_reason") or legacy_state.get("error") or boot_state.get("error") or "",
        "livekit_message": final_block_reason or legacy_state.get("livekit_message"),
        "gemini_status": legacy_state.get("gemini_status"),
        "voice_mode": legacy_state.get("voice_mode"),
        "restart_required_for_voice": bool(legacy_state.get("restart_required_for_voice")),
        "block_reason": final_block_reason,
        "missing_flags": final_missing_flags,
        "diagnostic_flags": legacy_state.get("diagnostic_flags") or [],
        "optional_degraded_services": get_degraded_services(),
        "state": final_state,
    }


def _update_livekit_runtime_state(
    *,
    livekit_current_slot: int | None = None,
    livekit_total_slots: int | None = None,
    livekit_status: str | None = None,
    livekit_worker_boot_status: str | None = None,
    livekit_ready_source: str | None = None,
    voice_mode: str | None = None,
    restart_required_for_voice: bool | None = None,
    livekit_message: str | None = None,
    log_reason: str = "",
) -> dict:
    with _runtime_lock:
        previous_status = _runtime_readiness.livekit_status
        previous_voice_mode = _runtime_readiness.voice_mode
        previous_restart_required = _runtime_readiness.restart_required_for_voice
        if livekit_total_slots is not None:
            _runtime_readiness.livekit_total_slots = int(livekit_total_slots)
        if livekit_current_slot is not None:
            _runtime_readiness.livekit_current_slot = int(livekit_current_slot)
            LIVEKIT_MANAGER.mark_current(int(livekit_current_slot))
        if livekit_status is not None:
            _runtime_readiness.livekit_status = str(livekit_status or "unknown").strip().lower() or "unknown"
        if livekit_worker_boot_status is not None:
            _runtime_readiness.livekit_worker_boot_status = str(livekit_worker_boot_status or "unknown").strip().lower() or "unknown"
        if livekit_ready_source is not None:
            _runtime_readiness.livekit_ready_source = str(livekit_ready_source or "unknown").strip() or "unknown"
        if voice_mode is not None:
            _runtime_readiness.voice_mode = str(voice_mode or "voice").strip().lower() or "voice"
        if restart_required_for_voice is not None:
            _runtime_readiness.restart_required_for_voice = bool(restart_required_for_voice)
        if livekit_message is not None:
            _runtime_readiness.livekit_message = str(livekit_message or "").strip()
        if (
            _runtime_readiness.voice_mode in {"text_only", "unavailable"}
            and _runtime_readiness.restart_required_for_voice
        ):
            _runtime_readiness.agent_ready = False
        _runtime_readiness.updated_at = _now_iso()
        data = _runtime_readiness.as_dict()

    if data.get("livekit_status") == "quota_exceeded":
        LIVEKIT_MANAGER.mark_quota_exceeded(data.get("livekit_current_slot"))
    elif data.get("livekit_status") == "auth_failed":
        LIVEKIT_MANAGER.mark_auth_failed(data.get("livekit_current_slot"))
    if (
        data.get("voice_mode") in {"text_only", "unavailable"}
        and bool(data.get("restart_required_for_voice"))
    ):
        _agent_ready.clear()
        _publish_backend_ready_if_possible()

    if previous_status != data.get("livekit_status"):
        _runtime_log(
            "LIVEKIT_STATUS_CHANGED",
            status=data.get("livekit_status"),
            slot=data.get("livekit_current_slot"),
            reason=log_reason or None,
        )
    if previous_voice_mode != data.get("voice_mode"):
        _runtime_log(
            "VOICE_MODE_CHANGED",
            mode=data.get("voice_mode"),
            reason=log_reason or None,
        )
    if previous_restart_required != data.get("restart_required_for_voice"):
        _runtime_log(
            "RESTART_REQUIRED_FOR_VOICE",
            value=str(bool(data.get("restart_required_for_voice"))).lower(),
        )
    _schedule_transcription_status_push(data)
    return data


def _extract_livekit_runtime_updates(payload: dict | None) -> dict[str, object]:
    data = dict(payload or {})
    updates: dict[str, object] = {}
    for key in (
        "livekit_current_slot",
        "livekit_total_slots",
        "livekit_status",
        "livekit_worker_boot_status",
        "livekit_ready_source",
        "voice_mode",
        "restart_required_for_voice",
        "livekit_message",
    ):
        if key in data:
            updates[key] = data.get(key)
    return updates


def _schedule_transcription_status_push(state: dict | None = None) -> None:
    payload = _augment_runtime_state(dict(state or _runtime_state_snapshot()))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(transcription_bus.send_status(payload))


def _degraded_runtime_ready_unlocked() -> bool:
    return bool(
        _runtime_readiness.voice_mode in {"text_only", "unavailable"}
        and _runtime_readiness.restart_required_for_voice
        and _runtime_readiness.livekit_status in {
            "all_failed",
            "quota_exceeded",
            "auth_failed",
            "dns_failed",
            "cert_failed",
            "connect_failed",
            "provider_failed",
        }
    )


def _build_text_only_backend_prompt(user_text: str, memory_context: str = "") -> str:
    normalized = str(user_text or "").strip()
    memory_block = str(memory_context or "").strip()
    memory_instruction = ""
    if memory_block:
        memory_instruction = (
            f"{memory_block}\n"
            "Use somente estes fatos se forem relevantes. Nao invente memorias. "
            "Nao exponha valor sensivel bruto; se houver memoria sensivel, responda com cautela e peca confirmacao clara.\n\n"
        )
    with _text_only_chat_lock:
        history = list(_text_only_chat_history[-TEXT_ONLY_MAX_HISTORY_MESSAGES:])
    if not history:
        return (
            f"{TEXT_ONLY_SYSTEM_PROMPT}\n\n"
            f"{memory_instruction}"
            f"Usuario: {normalized}\n"
            "Kira:"
        )
    history_lines = []
    for item in history:
        role = str(item.get("role") or "user").strip().lower()
        label = "Kira" if role == "assistant" else "Usuario"
        content = str(item.get("content") or "").strip()
        if content:
            history_lines.append(f"{label}: {content}")
    transcript = "\n".join(history_lines[-TEXT_ONLY_MAX_HISTORY_MESSAGES:])
    return (
        f"{TEXT_ONLY_SYSTEM_PROMPT}\n\n"
        f"{memory_instruction}"
        f"Contexto recente:\n{transcript}\n\n"
        f"Usuario: {normalized}\n"
        "Kira:"
    )


def _append_text_only_history(role: str, content: str) -> None:
    normalized = str(content or "").strip()
    if not normalized:
        return
    entry = {
        "role": "assistant" if str(role or "").strip().lower() == "assistant" else "user",
        "content": normalized,
    }
    with _text_only_chat_lock:
        _text_only_chat_history.append(entry)
        if len(_text_only_chat_history) > TEXT_ONLY_MAX_HISTORY_MESSAGES:
            del _text_only_chat_history[:-TEXT_ONLY_MAX_HISTORY_MESSAGES]


def _is_gemini_text_quota_error(exc: Exception | str) -> bool:
    text = str(exc or "").strip().lower()
    return any(
        fragment in text
        for fragment in (
            "429",
            "quota",
            "rate limit",
            "resource exhausted",
            "resource_exhausted",
            "too many requests",
        )
    )


def _is_gemini_text_auth_error(exc: Exception | str) -> bool:
    text = str(exc or "").strip().lower()
    return any(
        fragment in text
        for fragment in (
            "401",
            "403",
            "invalid api key",
            "api key not valid",
            "permission denied",
            "unauthorized",
            "forbidden",
            "invalid credentials",
        )
    )


def _extract_gemini_response_text(response: object) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return ""
    fragments: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        for part in parts or ():
            value = str(getattr(part, "text", "") or "").strip()
            if value:
                fragments.append(value)
    return "\n".join(fragment for fragment in fragments if fragment).strip()


def _generate_text_only_reply_sync(user_text: str, memory_context: str = "") -> str:
    return _gemini_text_sync(_build_text_only_backend_prompt(user_text, memory_context), reason="text_only_chat", tag="TEXT_ONLY_GEMINI")


def _gemini_text_sync(
    prompt: str,
    *,
    reason: str,
    tag: str,
    max_rotations: int = TEXT_ONLY_MAX_GEMINI_ROTATIONS,
    models: tuple[str, ...] = (),
    http_timeout_ms: int = TEXT_ONLY_CHAT_HTTP_TIMEOUT_MS,
    json_mode: bool = False,
    think: bool = True,
) -> str:
    """Texto pelo Gemini com a chave de texto (a voz usa outra); em cota ou chave recusada, gira para a próxima. Com
    `models`, erro do servidor (modelo lotado, fora do ar) tenta o próximo da lista.
    Rode no `_text_only_chat_executor` (uma thread só): a troca de chave não tem trava entre threads."""
    try:
        from google import genai
        from google.genai import types as genai_types
    except Exception as exc:
        raise RuntimeError("Pacote google-genai indisponivel para modo texto.") from exc

    last_error: Exception | None = None
    attempted_rotations = 0
    candidates = list(models) or [TEXT_ONLY_GEMINI_MODEL]
    model_index = 0

    while attempted_rotations < max_rotations:
        key, key_index = activate_current_gemini_api_key(
            logger_override=logger,
            reason=reason,
            key_type="text",
            update_env=False,
        )
        if not key:
            raise RuntimeError("Nenhuma chave Gemini de texto disponivel.")
        try:
            client = genai.Client(
                api_key=key,
                http_options=genai_types.HttpOptions(timeout=http_timeout_ms),
            )
            response = client.models.generate_content(
                model=candidates[model_index],
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json" if json_mode else None,
                    # think=False: resposta direta (conversa do convidado, receita), sem os segundos de "pensamento"
                    thinking_config=None if think or "2.5-pro" in candidates[model_index] else genai_types.ThinkingConfig(thinking_budget=0),
                ) if (json_mode or not think) else None,
            )
            reply = _extract_gemini_response_text(response)
            if not reply:
                raise RuntimeError("Gemini retornou resposta vazia no modo texto.")
            mark_current_gemini_key_working(logger_override=logger, key_type="text")
            logger.info(
                "%s_REPLY_OK key_index=%s model=%s",
                tag,
                key_index,
                candidates[model_index],
            )
            return reply
        except Exception as exc:
            last_error = exc
            attempted_rotations += 1
            if _is_gemini_text_quota_error(exc):
                logger.warning("%s_QUOTA key_index=%s error=%s", tag, key_index, exc)
                rotated = mark_current_gemini_key_quota_exceeded(
                    logger_override=logger,
                    key_type="text",
                    reason=str(exc),
                )
                if rotated is None:
                    break
                continue
            if _is_gemini_text_auth_error(exc):
                logger.warning("%s_AUTH_FAILED key_index=%s error=%s", tag, key_index, exc)
                rotated = rotate_to_next_usable_gemini_api_key(
                    logger_override=logger,
                    reason=f"{reason}_auth_failed",
                    key_type="text",
                )
                if rotated is None:
                    break
                continue
            if model_index + 1 < len(candidates):
                logger.warning("%s_MODEL_FAILED model=%s error=%s", tag, candidates[model_index], type(exc).__name__)
                model_index += 1
                continue
            logger.warning("%s_GENERIC_FAILED key_index=%s error=%s", tag, key_index, exc)
            break

    raise RuntimeError(str(last_error or "Falha ao gerar resposta em modo texto."))


def _drop_last_text_only_history_if(role: str) -> None:
    """Remove a ultima entrada do historico se for do papel indicado.

    Usado para limpar o turno do usuario quando a geracao falha/timeout,
    evitando que a proxima mensagem herde um contexto quebrado.
    """
    target = "assistant" if str(role or "").strip().lower() == "assistant" else "user"
    with _text_only_chat_lock:
        if _text_only_chat_history and _text_only_chat_history[-1].get("role") == target:
            _text_only_chat_history.pop()


async def _handle_text_only_chat_message(user_text: str, memory_context: str = "") -> str:
    normalized = str(user_text or "").strip()
    if not normalized:
        raise ValueError("Mensagem vazia.")
    _append_text_only_history("user", normalized)
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _text_only_chat_executor,
        _generate_text_only_reply_sync,
        normalized,
        str(memory_context or "").strip(),
    )
    try:
        reply = await asyncio.wait_for(future, timeout=TEXT_ONLY_CHAT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # Limpa o turno pendente do usuario para nao envenenar o proximo turno.
        # A thread em si e desbloqueada pelo timeout HTTP do client Gemini.
        _drop_last_text_only_history_if("user")
        logger.warning(
            "TEXT_ONLY_CHAT_TIMEOUT seconds=%s http_timeout_ms=%s",
            TEXT_ONLY_CHAT_TIMEOUT_SECONDS,
            TEXT_ONLY_CHAT_HTTP_TIMEOUT_MS,
        )
        raise
    except Exception:
        _drop_last_text_only_history_if("user")
        raise
    cleaned_reply = str(reply or "").strip()
    if not cleaned_reply:
        _drop_last_text_only_history_if("user")
        raise RuntimeError("Resposta vazia da Kira em modo texto.")
    _append_text_only_history("assistant", cleaned_reply)
    return cleaned_reply


def _update_agent_telemetry(payload: dict | None) -> dict[str, object]:
    data = dict(payload or {})
    with _runtime_lock:
        _agent_telemetry.clear()
        _agent_telemetry.update(data)
        return dict(_agent_telemetry)


def _agent_telemetry_snapshot() -> dict[str, object]:
    with _runtime_lock:
        return dict(_agent_telemetry)


def _load_agent_key_report() -> dict[str, object]:
    try:
        return GeminiKeyManager(config_path=AGENT_DIR / "api_keys_config.json").get_status_report()
    except Exception as exc:
        logger.warning("[SYSTEM_STATUS] Falha ao carregar status das chaves Gemini: %s", exc)
        return {
            "configured_keys": 0,
            "usable_keys": 0,
            "available_text_keys": 0,
            "available_tts_keys": 0,
            "current_text_index": None,
            "current_tts_index": None,
            "all_text_keys_in_cooldown": False,
            "all_tts_keys_in_cooldown": False,
            "key_details": [],
        }


def _update_runtime_readiness(**changes) -> dict:
    with _runtime_lock:
        for key, value in changes.items():
            if hasattr(_runtime_readiness, key):
                setattr(_runtime_readiness, key, value)
        _runtime_readiness.updated_at = _now_iso()
        data = _runtime_readiness.as_dict()
    _update_session_debug(
        requested_room_name=str(data.get("requested_room_name") or "").strip(),
        room_name=str(data.get("room_name") or "").strip(),
        last_event=str(data.get("event") or "").strip(),
        last_message=str(data.get("message") or "").strip(),
        last_error=str(data.get("error") or data.get("failure_reason") or "").strip(),
        last_stage=str(data.get("boot_state") or "").strip(),
    )
    effective_state = _augment_runtime_state(data)
    _sync_boot_orchestrator_from_runtime_state(effective_state, reason="runtime_update")
    _sync_runtime_status_manager_from_legacy(effective_state, already_augmented=True)
    _runtime_log("UI_STATUS_UPDATE", ui_status=data.get("ui_status"), boot_state=data.get("boot_state"), event=data.get("event"))
    return data


def _transition_boot_state(new_state: str, *, message: str = "", event: str = "", error: str = "", failure_reason: str = "") -> dict:
    with _runtime_lock:
        old_state = _runtime_readiness.boot_state
    _runtime_log("BOOT_STATE_CHANGE", old=old_state, new=new_state, event=event)
    update = {
        "boot_state": new_state,
        "event": event or new_state,
        "message": message or _runtime_readiness.message,
    }
    if error:
        update["error"] = error
    if failure_reason:
        update["failure_reason"] = failure_reason
    return _update_runtime_readiness(**update)


def _run_memory_boot_health_check() -> dict[str, object]:
    started = time.monotonic()
    logger.info("MEMORY_READY_CHECK_BEGIN timeout_s=%.1f", MEMORY_HEALTH_TIMEOUT_SECONDS)
    _update_runtime_readiness(
        memory_ready=False,
        memory_status="checking",
        memory_error="",
        memory_checked_at=time.time(),
        memory_elapsed_ms=None,
        event="MEMORY_READY_CHECK_BEGIN",
        message="Validando memoria da Kira...",
    )
    get_runtime_status_manager().set_memory_ready(False, status="checking", error=None, elapsed_ms=None)
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "test_memory_boot_health.py")]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=MEMORY_HEALTH_TIMEOUT_SECONDS + 2.0,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0 and MEMORY_BOOT_CONTEXT_PATH.exists():
            logger.info("MEMORY_READY_PUBLISHED elapsed_ms=%s cache=%s", elapsed_ms, MEMORY_BOOT_CONTEXT_PATH)
            state = _update_runtime_readiness(
                memory_ready=True,
                memory_status="ready",
                memory_error="",
                memory_checked_at=time.time(),
                memory_elapsed_ms=elapsed_ms,
                event="MEMORY_READY_PUBLISHED",
                message="Memoria pronta.",
            )
            get_runtime_status_manager().set_memory_ready(True, status="ready", error=None, elapsed_ms=elapsed_ms)
            return state
        error_text = output[-800:] if output else f"memory health exited with code {proc.returncode}"
        logger.error("MEMORY_READY_FAILED elapsed_ms=%s error=%s", elapsed_ms, error_text)
        state = _update_runtime_readiness(
            memory_ready=False,
            memory_status="failed",
            memory_error=error_text,
            memory_checked_at=time.time(),
            memory_elapsed_ms=elapsed_ms,
            event="MEMORY_READY_FAILED",
            message="Memoria com erro.",
        )
        get_runtime_status_manager().set_memory_ready(False, status="failed", error=error_text, elapsed_ms=elapsed_ms)
        return state
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error_text = "memory health timeout"
        logger.error("MEMORY_READY_TIMEOUT timeout_s=%.1f elapsed_ms=%s", MEMORY_HEALTH_TIMEOUT_SECONDS, elapsed_ms)
        state = _update_runtime_readiness(
            memory_ready=False,
            memory_status="timeout",
            memory_error=error_text,
            memory_checked_at=time.time(),
            memory_elapsed_ms=elapsed_ms,
            event="MEMORY_READY_TIMEOUT",
            message="Memoria demorou demais para iniciar.",
        )
        get_runtime_status_manager().set_memory_ready(False, status="timeout", error=error_text, elapsed_ms=elapsed_ms)
        return state


def _start_memory_boot_health_thread() -> None:
    if KIRA_BOOT_MINIMAL and not KIRA_MEMORY_REQUIRED:
        logger.info("MEMORY_BOOT_DEFERRED reason=boot_minimal")
        logger.info("MEMORY_LOAD_SKIPPED_BOOT_MINIMAL")
        get_runtime_status_manager().set_memory_ready(False, status="deferred", error=None, elapsed_ms=None)
        _update_runtime_readiness(
            memory_ready=False,
            memory_status="deferred",
            memory_error="",
            memory_checked_at=time.time(),
            memory_elapsed_ms=None,
            event="MEMORY_BOOT_DEFERRED",
            message="Memoria adiada pelo boot minimo.",
        )
        return
    thread = threading.Thread(
        target=_run_memory_boot_health_check,
        name="kira-memory-health",
        daemon=True,
    )
    thread.start()


def _core_session_ready_unlocked() -> bool:
    return bool(
        _runtime_readiness.session_started
        and _runtime_readiness.room_ready
        and _runtime_readiness.gemini_ready
        and _runtime_readiness.handlers_registered
        and _runtime_readiness.voice_pipeline_ready
        and _runtime_readiness.runtime_ready
    )


def _publish_backend_ready_if_possible() -> None:
    with _runtime_lock:
        voice_ready = _core_session_ready_unlocked()
        degraded_ready = _degraded_runtime_ready_unlocked()
        degraded_message = _runtime_readiness.livekit_message
        already_backend_ready = bool(_runtime_readiness.backend_ready)
        publish_key = _ready_publish_key_unlocked()
        already_published = _ready_republish_already_published_unlocked("SESSION_RUNTIME_READY", publish_key)
        functional_already_published = _ready_republish_already_published_unlocked("FUNCTIONAL_READY", publish_key)
        if voice_ready and not already_published:
            _ready_republish_mark_published_unlocked("SESSION_RUNTIME_READY", publish_key)
    if voice_ready:
        if already_published:
            _ready_republish_record_skip("SESSION_RUNTIME_READY", publish_key)
            if not functional_already_published:
                _publish_functional_ready_if_possible()
            return
        _update_runtime_readiness(
            agent_ready=True,
            backend_ready=True,
            ui_status="connecting",
            boot_state="runtime_ready",
            event="SESSION_RUNTIME_READY",
            message="Sessao conectada. Entregando saudacao inicial...",
            error="",
            failure_reason="",
        )
        _runtime_log("SESSION_RUNTIME_READY")
        _ready_info_log("SESSION_RUNTIME_READY_PUBLISHED")
        _publish_functional_ready_if_possible()
        return
    if not degraded_ready or already_backend_ready:
        return
    _update_runtime_readiness(
        agent_ready=False,
        backend_ready=True,
        ui_status="backend_ready",
        boot_state="backend_ready",
        event="DEGRADED_BACKEND_READY",
        message=degraded_message or "LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
        error="",
        failure_reason="",
    )
    _runtime_log("BACKEND_READY_DEGRADED")
    logger.info("TEXT_MODE_READY")
    logger.info("TEXT_MODE_RESTORED_AFTER_RESTART")
    logger.info("TEXT_FALLBACK_MODE_ENABLED reason=livekit_degraded")
    logger.info("SESSION_DEGRADED_TEXT_AVAILABLE voice_available=false")
    logger.info("FUNCTIONAL_READY_DEGRADED_LIVEKIT")


def schedule_post_boot_lazy_load() -> None:
    global _post_boot_lazy_load_started
    if KIRA_BOOT_MINIMAL:
        logger.info("POST_BOOT_LAZY_LOAD_SKIP reason=boot_minimal")
        logger.info("MANUS_LAZY_READY")
        logger.info("WHATSAPP_LAZY_READY")
        logger.info("BROWSER_LAZY_READY")
        logger.info("VISION_LAZY_READY")
        logger.info("MUSIC_LAZY_READY")
        logger.info("SYSTEM_VITALS_LAZY_READY")
        return
    if not KIRA_LAZY_LOAD_EXTRAS:
        logger.info("POST_BOOT_LAZY_LOAD_SKIP lazy_load_extras=false")
        return
    with _post_boot_lazy_load_lock:
        if _post_boot_lazy_load_started:
            return
        _post_boot_lazy_load_started = True

    def _runner() -> None:
        logger.info("POST_BOOT_LAZY_LOAD_BEGIN")
        logger.info("POST_BOOT_LAZY_LOAD_TRANSCRIPTION_LIGHT")
        logger.info("POST_BOOT_LAZY_LOAD_SYSTEM_VITALS_LIGHT")
        logger.info("SYSTEM_VITALS_LAZY_LOAD_DATA_ONLY")
        logger.info("SYSTEM_VITALS_AUTO_SHOW_BLOCKED reason=boot_or_lazy_load")
        logger.info("SYSTEM_VITALS_VISIBILITY_PRESERVED visible=false")
        logger.info("PANEL_VISIBILITY_PRESERVED panel=system_vitals visible=false")
        try:
            cfg = load_whatsapp_runtime_config()
            if bool(cfg.get("enabled", False)):
                _set_whatsapp_runtime_state("stopped", bridge_available=False)
                logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
                logger.info("WHATSAPP_LAZY_READY")
                logger.info("WHATSAPP_RUNTIME_STATE state=off")
            else:
                _set_whatsapp_runtime_state("disabled", bridge_available=False)
                logger.info("WHATSAPP_LAZY_SKIP disabled=true")
        except Exception as exc:
            logger.warning("WHATSAPP_LAZY_DEGRADED error=%s", exc)
        logger.info("VISION_LAZY_SKIP disabled=true")
        logger.info("BROWSER_LAZY_WAIT user_action_required")
        logger.info("MUSIC_LAZY_WAIT user_action_required")
        logger.info("MAINTENANCE_LAZY_WAIT user_action_required")
        logger.info("POST_BOOT_LAZY_LOAD_DONE")

    threading.Thread(target=_runner, name="KiraPostBootLazyLoad", daemon=True).start()


def _publish_functional_ready_if_possible() -> None:
    with _runtime_lock:
        ready = bool(
            _core_session_ready_unlocked()
            and _runtime_readiness.startup_greeting_done
        )
        publish_key = _ready_publish_key_unlocked()
        already_published = _ready_republish_already_published_unlocked("FUNCTIONAL_READY", publish_key)
        if ready and not already_published:
            _ready_republish_mark_published_unlocked("FUNCTIONAL_READY", publish_key)
    if not ready:
        return
    if already_published:
        _ready_republish_record_skip("FUNCTIONAL_READY", publish_key)
        return
    _agent_ready.set()
    _update_runtime_readiness(
        agent_ready=True,
        ui_ready=True,
        backend_ready=True,
        functional_ready=True,
        ui_status="session_ready",
        boot_state="functional_ready",
        event="FUNCTIONAL_READY",
        message="Kira pronta.",
        error="",
        failure_reason="",
    )
    _runtime_log("FUNCTIONAL_READY")
    _ready_info_log("FUNCTIONAL_READY_PUBLISHED")
    logger.info("TEXT_MODE_READY")
    logger.info("FUNCTIONAL_READY_WITH_TEXT_MODE")
    provider = get_selected_provider()
    logger.info(
        "SESSION_STATUS provider=%s text_ready=true voice_ready=true degraded=false",
        str(getattr(provider, "name", "") or "unknown"),
    )
    schedule_post_boot_lazy_load()
    _schedule_whatsapp_autostart_after_functional_ready()


def _schedule_whatsapp_autostart_after_functional_ready() -> None:
    """FASE 2B/C3: se o WhatsApp estiver enabled, inicia em background DEPOIS do
    FUNCTIONAL_READY. Nunca bloqueia voz/boot; falha do WhatsApp nao derruba a Kira."""
    global _whatsapp_autostart_done
    with _whatsapp_lock:
        if _whatsapp_autostart_done:
            return
        _whatsapp_autostart_done = True

    def _runner() -> None:
        try:
            # Quem decide é o botão da aba WhatsApp (ligado = sobe com a Kira). A ponte é Node/Baileys, leve; o antigo
            # WHATSAPP_AUTOSTART=false era do tempo em que ela abria um Chromium inteiro.
            enabled = is_whatsapp_enabled()
            logger.info("WHATSAPP_POST_FUNCTIONAL_READY_CHECK enabled=%s", str(enabled).lower())
            # Healthcheck so faz sentido quando o WhatsApp pode rodar.
            if enabled:
                _ensure_whatsapp_healthcheck_started()
            if not enabled:
                logger.info("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_SKIPPED reason=disabled")
                return
            if _is_whatsapp_process_running():
                logger.info("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_SKIPPED reason=already_running")
                return
            # Espera o pico do boot (LiveKit + Gemini + interface) passar antes de subir a ponte.
            # Ajustavel via KIRA_WHATSAPP_AUTOSTART_DELAY_S.
            try:
                _delay = max(float(os.getenv("KIRA_WHATSAPP_AUTOSTART_DELAY_S", "12") or "12"), 0.0)
            except Exception:
                _delay = 12.0
            if _delay > 0:
                logger.info("WHATSAPP_AUTOSTART_SETTLE_DELAY seconds=%.0f", _delay)
                time.sleep(_delay)
            logger.info("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_BEGIN")
            ok = ensure_whatsapp_started()
            if ok:
                logger.info("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_OK")
            else:
                logger.warning("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_FAILED reason=start_failed")
        except Exception as exc:
            logger.warning("WHATSAPP_AUTOSTART_AFTER_FUNCTIONAL_READY_FAILED reason=%s", exc)

    threading.Thread(target=_runner, daemon=True, name="whatsapp-autostart").start()


def _apply_agent_ready_phase(phase: str, payload: dict | None = None) -> dict:
    global _agent_restart_count, _agent_last_restart_at
    data = dict(payload or {})
    phase_key = str(phase or "ready").strip().lower() or "ready"
    room_name = str(data.get("room_name") or data.get("requested_room_name") or "").strip()
    message = str(data.get("message") or "").strip()
    logger.info("BACKEND_AGENT_PHASE_RECEIVED phase=%s", phase_key)
    startup_timing_mark(
        event=phase_key,
        timing_id=str(data.get("timing_id") or ""),
        room=room_name,
        source="agent",
        details={"phase": phase_key},
    )
    if phase_key in {
        "agent_process_spawned",
        "agent_imports_done",
        "job_accepted",
        "memory_loaded",
        "gemini_connect_start",
        "mic_stable_check_start",
        "mic_stable_check_ok",
        "mic_stable_check_cancel",
        "greeting_requested",
        "greeting_first_audio",
    }:
        return _runtime_state_snapshot()
    with _runtime_lock:
        current_requested_room = str(_runtime_readiness.requested_room_name or _runtime_readiness.room_name or "").strip()
        current_room_connecting = bool(_runtime_readiness.room_connecting)
        current_room_connected = bool(_runtime_readiness.room_connected)
    room_context_available = bool(
        room_name or current_requested_room or current_room_connecting or current_room_connected
    )

    update: dict = {
        "event": str(data.get("event") or f"API_AGENT_{phase_key.upper()}"),
        "error": "",
        "failure_reason": "",
    }
    if room_name:
        update["room_name"] = room_name

    if phase_key in {"session_recovering", "recovering"}:
        update.update(
            boot_recovery_active=True,
            ui_status="connecting",
            backend_ready=False,
            functional_ready=False,
            ui_ready=False,
            event="SESSION_RECOVERING",
            message=message or "Recuperando sessao da Kira...",
        )
        return _update_runtime_readiness(**update)

    logger.info("[AGENT_READY_PHASE_RECEIVED] phase=%s", phase_key)
    if phase_key in {"worker_http_ready"}:
        logger.info("AGENT_READY_NOTIFY_RECEIVED phase=%s", phase_key)
        result = mark_agent_worker_ready(source="agent_notify", phase=phase_key)
        logger.info("[WORKER_HTTP_READY_RECEIVED] worker_ready_latched=true")
        _runtime_log("AGENT_WORKER_HTTP_READY", phase=phase_key)
        return result

    if phase_key in {"ready", "worker_ready", "agent_ready", "backend_ready"}:
        logger.info("AGENT_READY_NOTIFY_RECEIVED phase=%s", phase_key)
        result = mark_agent_worker_ready(source="agent_notify", phase=phase_key)
        logger.info("[WORKER_READY_RECEIVED]")
        _runtime_log("AGENT_WORKER_READY", phase=phase_key)
        logger.info("[AGENT_READY_FLAGS_UPDATED] phase=%s", phase_key)
        return result

    if phase_key in {"room_connected", "session_room_connected", "room_ready"}:
        logger.info("BOOT_REAL_SESSION_ROOM_CONNECTED room=%s", room_name or current_requested_room or "unknown")
        update.update(
            agent_connected=True,
            agent_ready=True,
            room_connecting=False,
            room_connected=True,
            room_ready=True,
            session_started=True,
            ui_status="connecting",
            boot_state="room_ready",
            event="SESSION_ROOM_CONNECTED",
            message=message or "Room conectada. Aguardando Gemini e pipeline de voz.",
        )
        return _update_runtime_readiness(**update)

    if phase_key in {"room_ready_early"}:
        logger.info("[KIRA READY STATE] ignoring room_ready_early phase=%s room_context=%s", phase_key, room_context_available)
        return _runtime_state_snapshot()

    if phase_key in {"gemini_ready"}:
        logger.info("BOOT_REAL_GEMINI_READY room=%s", room_name or current_requested_room or "unknown")
        update.update(
            agent_connected=True,
            agent_ready=True,
            gemini_ready=True,
            gemini_connecting=False,
            ui_status="connecting",
            boot_state="gemini_ready",
            event="GEMINI_READY",
            message=message or "Gemini pronto.",
        )
        was_recovering = bool(_runtime_state_snapshot().get("boot_recovery_active"))
        result = _update_runtime_readiness(**update)
        if was_recovering:
            logger.info("SESSION_RECOVERY_GEMINI_READY")
        return result

    if phase_key in {"gemini_ready_early"}:
        logger.info("[KIRA READY STATE] ignoring gemini_ready_early phase=%s room_context=%s", phase_key, room_context_available)
        return _runtime_state_snapshot()

    if phase_key in {"voice_pipeline_ready"}:
        logger.info("BOOT_REAL_VOICE_PIPELINE_READY room=%s", room_name or current_requested_room or "unknown")
        update.update(
            agent_connected=True,
            agent_ready=True,
            voice_pipeline_ready=True,
            ui_status="connecting",
            boot_state="voice_pipeline_ready",
            event="VOICE_PIPELINE_READY",
            message=message or "Pipeline de voz pronto.",
        )
        was_recovering = bool(_runtime_state_snapshot().get("boot_recovery_active"))
        result = _update_runtime_readiness(**update)
        if was_recovering:
            logger.info("SESSION_RECOVERY_VOICE_PIPELINE_READY")
        logger.info("BACKEND_VOICE_PIPELINE_READY_APPLIED")
        return result

    if phase_key in {"runtime_ready", "session_runtime_ready"}:
        logger.info("BOOT_REAL_SESSION_RUNTIME_READY room=%s", room_name or current_requested_room or "unknown")
        update.update(
            agent_connected=True,
            agent_ready=True,
            handlers_registered=True,
            runtime_ready=True,
            backend_ready=True,
            boot_recovery_active=False,
            boot_state="runtime_ready",
            ui_status="connecting",
            event="SESSION_RUNTIME_READY",
            message=message or "Sessao conectada. Entregando saudacao inicial...",
        )
        result = _update_runtime_readiness(**update)
        _ready_info_log("SESSION_RUNTIME_READY_PUBLISHED")
        _publish_backend_ready_if_possible()
        return result

    if phase_key in {"runtime_ready_early"}:
        logger.info("[KIRA READY STATE] ignoring runtime_ready_early phase=%s room_context=%s", phase_key, room_context_available)
        return _runtime_state_snapshot()

    if phase_key in {"functional_ready", "ui_ready", "ready"}:
        logger.info("BOOT_REAL_FUNCTIONAL_READY room=%s", room_name or current_requested_room or "unknown")
        was_recovering = bool(_runtime_state_snapshot().get("boot_recovery_active"))
        update.update(
            agent_connected=True,
            agent_ready=True,
            backend_ready=True,
            startup_greeting_done=True,
            ui_ready=True,
            boot_recovery_active=False,
            boot_state="functional_ready",
            ui_status="session_ready",
            event="FUNCTIONAL_READY",
            message=message or "Kira pronta.",
        )
        result = _update_runtime_readiness(**update)
        _agent_restart_count = 0
        _agent_last_restart_at = 0.0
        _agent_ready.set()
        _ready_info_log("FUNCTIONAL_READY_PUBLISHED")
        if was_recovering:
            logger.info("SESSION_RECOVERY_FUNCTIONAL_READY")
            logger.info("SESSION_RECOVERY_OK")
        _publish_functional_ready_if_possible()
        return result

    if phase_key in {"failed"}:
        update.update(
            event="AGENT_FAILED",
            message=message or "Falha reportada pelo agente.",
            failure_reason=str(data.get("reason") or data.get("error") or message or "agent_failed"),
        )
        logger.info("[AGENT_READY_FLAGS_UPDATED] phase=%s", phase_key)
        return _update_runtime_readiness(**update)

    logger.warning("[AGENT_READY_PHASE_INVALID] phase=%s", phase_key)
    update.update(
        ui_status="connecting",
        event=str(data.get("event") or f"API_AGENT_{phase_key.upper()}"),
        message=message or "Agente respondeu ao servidor.",
    )
    return _update_runtime_readiness(**update)


_agent_worker_ready_latch_lock = threading.Lock()


def mark_agent_worker_ready(source: str, phase: str = "worker_ready", pid: int | None = None) -> dict:
    normalized_source = str(source or "unknown").strip() or "unknown"
    normalized_phase = str(phase or "worker_ready").strip().lower() or "worker_ready"
    with _agent_worker_ready_latch_lock:
        already_ready = bool(_runtime_state_snapshot().get("agent_worker_ready"))
        _update_livekit_runtime_state(
            livekit_status="ready",
            livekit_worker_boot_status="ready",
            livekit_ready_source=f"worker_ready:{normalized_source}",
            voice_mode="voice",
            restart_required_for_voice=False,
            livekit_message=_livekit_runtime_message_for_status("ready"),
            log_reason=f"worker_ready:{normalized_source}",
        )
        update = {
            "agent_process_spawned": True,
            "agent_connected": True,
            "agent_worker_ready": True,
            "worker_ready_received": True,
            "agent_ready_phase": "worker_ready",
            "agent_probe_ok": True,
            "agent_ready": True,
            "backend_ready": True,
            "room_connecting": False,
            "boot_recovery_active": False,
            "boot_state": "backend_ready",
            "ui_status": "backend_ready",
            "event": "AGENT_WORKER_READY",
            "message": "Worker LiveKit iniciado e healthcheck ativo.",
            "error": "",
            "failure_reason": "",
        }
        if pid is not None:
            update["agent_pid"] = pid
        state = _update_runtime_readiness(**update)
        _agent_ready.set()
        try:
            get_runtime_status_manager().set_worker_ready(True)
        except Exception as exc:
            logger.warning("[RUNTIME_STATUS_SYNC] worker_ready_failed reason=%s", exc)
        try:
            boot_orchestrator.set_flag("backend_http_ok", True)
            boot_orchestrator.set_flag("livekit_server_ok", True)
            boot_orchestrator.set_flag("agent_process_spawned", True)
            boot_orchestrator.set_flag("agent_worker_ready", True)
            boot_orchestrator.set_phase(BootPhase.SYSTEM_READY, details=f"worker_ready:{normalized_source}")
        except Exception as exc:
            logger.warning("[BOOT_SYNC] falha: %s", exc)
        snapshot = _augment_runtime_state(dict(state))
        _sync_boot_orchestrator_from_runtime_state(snapshot, reason=f"worker_ready:{normalized_source}")
        if bool(snapshot.get("boot_recovery_active")):
            logger.info("SESSION_RECOVERY_WORKER_READY")
        logger.info(
            "AGENT_WORKER_READY_LATCHED source=%s phase=%s pid=%s already_ready=%s",
            normalized_source,
            normalized_phase,
            pid if pid is not None else "",
            str(already_ready).lower(),
        )
        logger.info(
            "KIRA_READY_STATE backend=%s livekit=%s agent_worker_ready=%s agent_ok=%s pre_start_ready=%s can_start_session=%s block_reason=%s",
            snapshot.get("backend_ready"),
            snapshot.get("livekit_status"),
            snapshot.get("agent_worker_ready"),
            snapshot.get("agent_ok"),
            snapshot.get("pre_start_ready"),
            snapshot.get("can_start_session"),
            snapshot.get("block_reason"),
        )
        _runtime_log("AGENT_WORKER_READY_LATCHED", source=normalized_source, phase=normalized_phase)
        return snapshot


def mark_agent_process_dead(exit_code: int | None = None, reason: str = "process_died") -> dict:
    detail = f"Agent encerrou inesperadamente exit_code={exit_code}" if exit_code is not None else "Agent encerrou inesperadamente"
    _agent_ready.clear()
    logger.error("AGENT_PROCESS_DIED exit_code=%s", exit_code if exit_code is not None else "")
    state = _update_runtime_readiness(
        agent_process_spawned=True,
        agent_process_dead=True,
        agent_connected=False,
        agent_ready=False,
        agent_worker_ready=False,
        worker_ready_received=False,
        agent_probe_ok=False,
        agent_error=True,
        agent_last_error=detail,
        agent_heartbeat_ok=False,
        agent_heartbeat_status="dead",
        agent_heartbeat_stale_since=0.0,
        agent_status="recovering",
        room_connecting=False,
        gemini_connecting=False,
        gemini_ready=False,
        voice_pipeline_ready=False,
        runtime_ready=False,
        functional_ready=False,
        ui_ready=False,
        boot_recovery_active=True,
        ui_status="recovering",
        event="AGENT_PROCESS_DIED",
        message="Agent caiu. Recuperando...",
        error=detail,
        failure_reason=reason,
    )
    logger.info("AGENT_STATE_SYNCED_DEAD")
    logger.info("AGENT_UI_STATUS_UPDATED status=recovering")
    _schedule_transcription_status_push(state)
    return _augment_runtime_state(dict(state))


def mark_agent_heartbeat_stale(age_ms: int) -> dict:
    watched_proc = _agent_proc
    process_alive = bool(watched_proc is not None and watched_proc.poll() is None)
    logger.warning("AGENT_HEARTBEAT_STALE age_ms=%s", int(age_ms))
    if process_alive:
        logger.info("AGENT_PROCESS_STILL_ALIVE_AFTER_HEARTBEAT_STALE pid=%s", watched_proc.pid)
        logger.info("AGENT_DEATH_FALSE_POSITIVE_AVOIDED reason=heartbeat_stale_process_alive")
        state = _update_runtime_readiness(
            agent_heartbeat_age_ms=int(age_ms),
            agent_heartbeat_ok=False,
            agent_heartbeat_status="stale",
            agent_heartbeat_stale_since=time.time(),
            agent_status="degraded",
            event="AGENT_HEARTBEAT_STALE",
            message="Heartbeat atrasado; processo do agent continua vivo.",
        )
        return _augment_runtime_state(state)
    exit_code = watched_proc.poll() if watched_proc is not None else None
    logger.error("AGENT_DEATH_CONFIRMED exit_code=%s reason=heartbeat_stale_process_not_alive", exit_code)
    return mark_agent_process_dead(exit_code=exit_code, reason="heartbeat_stale_process_not_alive")


def _consume_expected_agent_exit(pid: int) -> bool:
    with _livekit_failed_slots_lock:
        if int(pid) not in _agent_expected_exit_pids:
            return False
        _agent_expected_exit_pids.discard(int(pid))
        return True


def _get_assistant_runtime_state() -> dict:
    with _assistant_lock:
        state = dict(_assistant_runtime_state)
    if not state.get("updated_at"):
        state["updated_at"] = _now_iso()
    state["action_allowed"] = bool(
        state.get("mode_enabled")
        and state.get("screen_share_active")
        and state.get("control_enabled")
    )
    return state


def _set_assistant_runtime_state(*, mode_enabled=None, screen_share_active=None, control_enabled=None) -> dict:
    with _assistant_lock:
        if mode_enabled is not None:
            _assistant_runtime_state["mode_enabled"] = bool(mode_enabled)
        if screen_share_active is not None:
            _assistant_runtime_state["screen_share_active"] = bool(screen_share_active)
        if control_enabled is not None:
            _assistant_runtime_state["control_enabled"] = bool(control_enabled)
        _assistant_runtime_state["updated_at"] = _now_iso()
    return _get_assistant_runtime_state()


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _append_voice_validation_log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {message}\n"
    _append_line(VOICE_VALIDATION_LOG_PATH, line)
    _append_line(VOICE_RUNTIME_LOG_PATH, line)


def _set_whatsapp_runtime_state(state: str, *, last_error=None, bridge_available=None) -> None:
    with _whatsapp_lock:
        _whatsapp_runtime_state["state"] = str(state or "unknown").strip().lower() or "unknown"
        if last_error is not None:
            _whatsapp_runtime_state["last_error"] = str(last_error) if last_error else None
        if bridge_available is not None:
            _whatsapp_runtime_state["bridge_available"] = bool(bridge_available)
        _whatsapp_runtime_state["updated_at"] = _now_iso()


def _default_whatsapp_runtime_config() -> dict:
    return {
        "enabled": True,
        "mode": "assistant",
        "last_error": None,
        "updated_at": _now_iso(),
    }


def save_whatsapp_runtime_config(cfg: dict) -> dict:
    payload = _default_whatsapp_runtime_config()
    payload["enabled"] = bool(cfg.get("enabled", payload["enabled"]))
    payload["mode"] = str(cfg.get("mode", payload["mode"]) or "assistant").strip().lower() or "assistant"
    payload["last_error"] = cfg.get("last_error")
    payload["updated_at"] = _now_iso()
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    WHATSAPP_RUNTIME_CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def load_whatsapp_runtime_config() -> dict:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    if not WHATSAPP_RUNTIME_CONFIG_PATH.exists():
        cfg = _default_whatsapp_runtime_config()
        save_whatsapp_runtime_config(cfg)
        return cfg
    try:
        raw = json.loads(WHATSAPP_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = _default_whatsapp_runtime_config()
        save_whatsapp_runtime_config(cfg)
        return cfg
    cfg = _default_whatsapp_runtime_config()
    cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))
    cfg["mode"] = str(raw.get("mode", cfg["mode"]) or "assistant").strip().lower() or "assistant"
    cfg["last_error"] = raw.get("last_error")
    cfg["updated_at"] = str(raw.get("updated_at") or _now_iso())
    return cfg


def is_whatsapp_enabled() -> bool:
    return bool(is_optional_enabled("whatsapp") and load_whatsapp_runtime_config().get("enabled", False))


def get_whatsapp_mode() -> str:
    return str(load_whatsapp_runtime_config().get("mode", "assistant"))


def set_whatsapp_mode(mode: str) -> str:
    cfg = load_whatsapp_runtime_config()
    cfg["mode"] = str(mode or "assistant").strip().lower() or "assistant"
    cfg = save_whatsapp_runtime_config(cfg)
    return str(cfg["mode"])


def set_whatsapp_enabled(value: bool) -> dict:
    cfg = load_whatsapp_runtime_config()
    cfg["enabled"] = bool(value)
    cfg["last_error"] = None
    cfg = save_whatsapp_runtime_config(cfg)
    _set_whatsapp_runtime_state(
        "bridge_starting" if bool(value) else "disabled",
        last_error=None,
        bridge_available=False if not value else None,
    )
    return cfg


def _is_whatsapp_process_running() -> bool:
    return _whatsapp_proc is not None and _whatsapp_proc.poll() is None


def _whatsapp_bridge_headers() -> dict:
    return {"X-Kira-Internal": KIRA_INTERNAL_TOKEN}


def _whatsapp_account(me: object) -> dict:
    from services.whatsapp_overview import account

    return account(me)


def _read_whatsapp_bridge_status(timeout_seconds: float = 1.5) -> dict:
    try:
        import httpx
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get("http://127.0.0.1:5050/status")
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _request_whatsapp_bridge_start() -> None:
    _set_whatsapp_runtime_state("bridge_starting", bridge_available=False)
    threading.Thread(target=_trigger_whatsapp_bridge, daemon=True).start()


def _wait_for_whatsapp_bridge_ready(timeout_seconds: float = 8.0, poll_seconds: float = 0.5) -> dict:
    deadline = time.monotonic() + max(timeout_seconds, poll_seconds)
    last_status: dict = {}
    while time.monotonic() < deadline:
        last_status = _read_whatsapp_bridge_status(timeout_seconds=1.5)
        if last_status:
            return last_status
        time.sleep(poll_seconds)
    return last_status


def get_whatsapp_runtime_state() -> dict:
    from services.whatsapp_runtime_service import get_whatsapp_status

    enabled = is_whatsapp_enabled()
    process_running = _is_whatsapp_process_running()
    _wa_pid = getattr(_whatsapp_proc, "pid", None) if process_running else None
    if not process_running:
        lightweight = get_whatsapp_status()
        lightweight["enabled"] = enabled
        lightweight["mode"] = get_whatsapp_mode()
        lightweight["state"] = "disabled" if not enabled else lightweight.get("state", "off")
        lightweight["bridge_status"] = lightweight["state"]
        lightweight["status_label"] = str(lightweight["state"]).replace("_", " ").upper()
        # FASE 2B/C1: fonte unica de status — campos coerentes mesmo com bridge offline.
        lightweight["process_running"] = False
        lightweight["bridge_available"] = False
        lightweight["connected"] = False
        lightweight["logged_in"] = None
        lightweight["send_ready"] = False
        lightweight["requiresStart"] = bool(enabled)
        lightweight["needs_qr"] = False
        lightweight["requires_user_action"] = False
        lightweight["user_action"] = ""
        lightweight["window_visible"] = False
        lightweight["window_state"] = "unavailable"
        lightweight["window_bounds"] = None
        lightweight["pid"] = None
        lightweight["ports"] = {}
        lightweight["source"] = "backend_lightweight"
        lightweight["stale"] = False
        lightweight["last_checked_at"] = _now_iso()
        return lightweight
    bridge_status = _read_whatsapp_bridge_status()
    bridge_available = bool(process_running and bridge_status)
    connected = bool(bridge_status.get("connected"))
    controller_active = bool(bridge_status.get("controller_active"))
    # O bridge "WhatsApp Agent" expõe apenas `connected`, e só o seta como True
    # APÓS login + busca pronta (whatsapp_controller: _wait_login + _wait_search_ready
    # antes de on_connected). Ele NAO envia search_ready/send_ready/details. Entao,
    # quando connected=True e o bridge nao informa esses flags, derivamos de
    # `connected` — senao a UI/teste viam send_ready=false (falso-negativo) com o
    # WhatsApp ja logado e pronto, bloqueando o envio.
    search_ready = bool(
        bridge_status.get("search_ready")
        or (
            isinstance(bridge_status.get("details"), dict)
            and bridge_status.get("details", {}).get("search_ready")
        )
        or connected
    )
    send_ready = bool(bridge_status.get("send_ready") or connected)
    _bridge_details = bridge_status.get("details") if isinstance(bridge_status.get("details"), dict) else {}
    _bridge_details_state = str(_bridge_details.get("state") or "").strip().lower()
    # FASE 2B.4: a bridge pode reportar session_state="unknown" no topo enquanto details.state
    # ja tem o estado real (ex.: loading_stuck). Usa details.state como fallback.
    session_state = str(bridge_status.get("session_state") or bridge_status.get("state") or "").strip().lower()
    if session_state in ("", "unknown") and _bridge_details_state:
        session_state = _bridge_details_state
    with _whatsapp_lock:
        runtime_state = str(_whatsapp_runtime_state.get("state") or "").strip().lower()
        last_error = _whatsapp_runtime_state.get("last_error")
    if not enabled:
        state = "disabled"
    elif connected:
        state = "session_connected"
    elif session_state:
        state = session_state
    elif bridge_available:
        state = "bridge_online"
    elif last_error and not process_running:
        state = "error"
    elif process_running:
        state = runtime_state if runtime_state in {
            "booting",
            "bridge_starting",
            "connecting",
            "session_connecting",
            "waiting_qr",
            "authenticating",
            "interface_ready",
        } else "bridge_starting"
    else:
        state = "disconnected"
    bridge_state = (
        session_state
        or ("connected" if connected else "")
        or ("bridge_online" if bridge_available else "")
        or ("bridge_starting" if process_running and enabled else "")
        or ("disabled" if not enabled else "disconnected")
    )
    details = bridge_status.get("details") if isinstance(bridge_status.get("details"), dict) else {}
    window_visible = bool(bridge_status.get("window_visible", details.get("window_visible", False)))
    window_state = str(bridge_status.get("window_state") or details.get("window_state") or ("visible" if window_visible else "hidden"))
    window_bounds = bridge_status.get("window_bounds") or details.get("window_bounds")
    recovering = bool(details.get("recovering")) or state in {"reconnecting", "connecting", "bridge_starting"}
    # Idem: o bridge simples nao manda esses flags de browser/pagina. Com
    # connected=True (logado + pronto), derivamos de `connected` pra refletir o
    # estado real e nao mostrar tudo "morto"/"deslogado" na UI.
    browser_alive = bool(details.get("browser_alive", connected))
    context_alive = bool(details.get("context_alive", browser_alive))
    page_alive = bool(details.get("page_alive", connected))
    logged_in = bool(details.get("logged_in", connected))
    page_ready = bool(details.get("page_ready", page_alive))
    last_error_code = details.get("last_error_code")
    # FASE 2B.2/C3: acao manual de QR exposta no topo do payload.
    needs_qr = bool(details.get("needs_qr")) or state == "waiting_qr"
    requires_user_action = bool(details.get("requires_user_action")) or needs_qr
    user_action = str(details.get("user_action") or ("scan_qr" if needs_qr else ""))
    return {
        "enabled": enabled,
        "mode": get_whatsapp_mode(),
        "process_running": process_running,
        "bridge_available": bridge_available,
        "controller_active": controller_active,
        "connected": connected,
        "logged_in": logged_in,
        "page_ready": page_ready,
        "search_ready": search_ready,
        "send_ready": send_ready,
        "needs_qr": needs_qr,
        "requires_user_action": requires_user_action,
        "user_action": user_action,
        "window_visible": window_visible,
        "window_state": window_state,
        "window_bounds": window_bounds,
        "browser_alive": browser_alive,
        "context_alive": context_alive,
        "page_alive": page_alive,
        "recovering": recovering,
        "bridge_status": bridge_state,
        "state": state,
        "session_state": session_state or state,
        "last_error": bridge_status.get("last_error") or last_error,
        "last_error_code": last_error_code,
        "qr": bridge_status.get("qr") if needs_qr or session_state == "waiting_qr" else None,
        # nome do perfil ou, se ele for só "." ou vazio, o número mascarado (o número inteiro não vai para a tela)
        "me": _whatsapp_account(bridge_status.get("me")),
        "details": details,
        "status_label": state.replace("_", " ").upper(),
        "requiresStart": bool(enabled) and state not in {"session_connected", "bridge_online"} and not bridge_available,
        "pid": _wa_pid,
        "ports": {"bridge": 5050, "admin": 5100} if bridge_available else {"admin": 5100},
        "source": "bridge_live" if bridge_available else "backend_process",
        "stale": bool(process_running and not bridge_status),
        "last_checked_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _read_whatsapp_bridge_log(timeout_seconds: float = 2.0) -> list[dict]:
    try:
        import httpx

        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get("http://127.0.0.1:5050/log", headers=_whatsapp_bridge_headers())
            if resp.status_code != 200:
                return []
            payload = resp.json()
    except Exception:
        return []
    raw_logs = payload.get("log") if isinstance(payload, dict) else []
    if not isinstance(raw_logs, list):
        return []
    parsed: list[dict] = []
    for index, item in enumerate(raw_logs[-100:], start=1):
        text = str(item or "").strip()
        if not text:
            continue
        parsed.append(
            {
                "id": f"bridge-log-{index}",
                "type": "info",
                "message": text,
                "timestamp": "",
            }
        )
    return parsed


def _whatsapp_healthcheck_loop() -> None:
    """FASE 2B/C5: healthcheck leve. So age quando enabled. Recupera processo/bridge
    com cooldown e limite de falhas consecutivas para nao gastar CPU nem ficar
    acordando processo indefinidamente."""
    interval_s = 30.0
    cooldown_s = 60.0
    max_consecutive_failures = 3
    consecutive_failures = 0
    not_connected_streak = 0
    last_recovery_at = 0.0
    logged_disabled = False
    # ── Anti-loop do bridge (fix da espiral de restart) ───────────────────────
    # O bridge precisa de TEMPO pra autenticar (o _wait_login dele permite 300s;
    # sob carga do boot a auth passa de 60s). O threshold antigo (2 checagens
    # ~60s) matava o bridge no MEIO da autenticacao -> loop infinito de restart e
    # nunca conectava. Agora: so considera "travado" apos ~2.5min nao-conectado
    # (graca), e LIMITA os restarts (depois disso para de matar e deixa o bridge
    # autenticar / mostrar QR sozinho).
    # Graca AUMENTADA: limpo o bridge conecta em ~40s, mas SOB A CARGA do boot/sessao
    # da Kira (LiveKit+Gemini+UI+o Chromium do bridge competindo) o page.goto do WhatsApp
    # Web fica lento e passa de 150s. O threshold antigo (150s) matava o bridge no meio da
    # conexao -> nunca conectava. 10 x 30s = ~300s da tempo de conectar sob carga.
    not_connected_threshold = 10         # 10 x 30s = ~300s de graca p/ autenticar
    max_stuck_restarts = 2               # apos isso, NAO mata mais (evita espiral)
    stuck_restart_count = 0
    while True:
        try:
            time.sleep(interval_s)
            if not is_whatsapp_enabled():
                # Modulo desligado: nao acorda nada. Loga uma vez por transicao p/ disabled.
                if not logged_disabled:
                    logger.info("WHATSAPP_HEALTHCHECK_SKIPPED_DISABLED")
                    logged_disabled = True
                consecutive_failures = 0
                continue
            logged_disabled = False
            now = time.monotonic()
            if consecutive_failures >= max_consecutive_failures:
                if now - last_recovery_at < cooldown_s:
                    continue
                consecutive_failures = 0  # fim do cooldown, tenta de novo
            process_running = _is_whatsapp_process_running()
            if not process_running:
                logger.warning("WHATSAPP_HEALTHCHECK_PROCESS_DEAD")
                logger.info("WHATSAPP_HEALTHCHECK_RECOVERY_ATTEMPT target=process")
                last_recovery_at = now
                ok = ensure_whatsapp_started()
                if ok:
                    logger.info("WHATSAPP_HEALTHCHECK_RECOVERY_OK target=process")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    logger.warning("WHATSAPP_HEALTHCHECK_RECOVERY_FAILED target=process attempts=%d", consecutive_failures)
                continue
            bridge_status = _read_whatsapp_bridge_status(timeout_seconds=2.0)
            if bridge_status:
                consecutive_failures = 0
                # Bridge HTTP up — mas o controller pode estar com o Chromium
                # PENDURADO (connected=false). So reinicia se ficar nao-conectado
                # por MUITO tempo (graca ~150s, p/ a autenticacao terminar) E ainda
                # nao estourou o limite de restarts (senao vira espiral que mata o
                # bridge no meio da auth e nunca conecta).
                if not bool((bridge_status or {}).get("connected")):
                    bridge_state_name = str((bridge_status or {}).get("state") or "").strip().lower()
                    if bridge_state_name in {"waiting_qr", "authenticating", "auth_required"}:
                        not_connected_streak = 0
                        logger.info(
                            "WHATSAPP_HEALTHCHECK_AUTH_STATE state=%s action=no_restart",
                            bridge_state_name,
                        )
                        continue
                    not_connected_streak += 1
                    logger.warning(
                        "WHATSAPP_HEALTHCHECK_NOT_CONNECTED streak=%d/%d restarts=%d/%d",
                        not_connected_streak, not_connected_threshold,
                        stuck_restart_count, max_stuck_restarts,
                    )
                    if not_connected_streak >= not_connected_threshold and (now - last_recovery_at) >= cooldown_s:
                        if stuck_restart_count >= max_stuck_restarts:
                            # Ja tentou o suficiente — PARA de matar. Deixa o bridge
                            # autenticar/mostrar QR por conta. (Evita o loop infinito.)
                            logger.warning(
                                "WHATSAPP_HEALTHCHECK_RESTART_GIVE_UP restarts=%d — deixando o bridge autenticar/QR sozinho (ver bridge_runtime.log)",
                                stuck_restart_count,
                            )
                            not_connected_streak = 0
                        else:
                            stuck_restart_count += 1
                            logger.info(
                                "WHATSAPP_HEALTHCHECK_RESTART_STUCK_BRIDGE reason=not_connected attempt=%d",
                                stuck_restart_count,
                            )
                            last_recovery_at = now
                            not_connected_streak = 0
                            try:
                                stop_whatsapp()
                            except Exception:
                                pass
                            time.sleep(2.0)
                            ensure_whatsapp_started()
                else:
                    # Conectou: reseta tudo (graca + cap) p/ futuras quedas reais.
                    not_connected_streak = 0
                    stuck_restart_count = 0
                continue
            logger.warning("WHATSAPP_HEALTHCHECK_BRIDGE_DOWN")
            logger.info("WHATSAPP_HEALTHCHECK_RECOVERY_ATTEMPT target=bridge")
            last_recovery_at = now
            _request_whatsapp_bridge_start()
            # confirma se a bridge respondeu apos a tentativa
            if _read_whatsapp_bridge_status(timeout_seconds=2.0):
                logger.info("WHATSAPP_HEALTHCHECK_RECOVERY_OK target=bridge")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                logger.warning("WHATSAPP_HEALTHCHECK_RECOVERY_FAILED target=bridge attempts=%d", consecutive_failures)
        except Exception as exc:
            logger.error("[WHATSAPP] Erro em healthcheck: %s", exc)


def _ensure_whatsapp_healthcheck_started() -> None:
    global _whatsapp_healthcheck_started
    with _whatsapp_lock:
        if _whatsapp_healthcheck_started:
            return
        _whatsapp_healthcheck_started = True
    logger.info("WHATSAPP_HEALTHCHECK_STARTED interval_s=30 cooldown_s=60")
    threading.Thread(target=_whatsapp_healthcheck_loop, daemon=True, name="whatsapp-healthcheck").start()


async def _ensure_agent_dispatch(
    room_name: str,
    credential: LiveKitCredential,
    *,
    recreate_existing: bool = False,
) -> dict | None:
    if not AGENT_NAME:
        return None
    try:
        from livekit.api import CreateAgentDispatchRequest, CreateRoomRequest, LiveKitAPI
    except Exception as exc:
        raise RuntimeError("Pacote livekit-api indisponivel para criar dispatch.") from exc

    import asyncio
    room_key = str(room_name or "").strip().lower()
    wait_for_existing = False
    with _dispatching_rooms_lock:
        if room_key in _dispatching_rooms:
            wait_for_existing = True
        else:
            _dispatching_rooms.add(room_key)
    if wait_for_existing:
        logger.info("LIVEKIT_DISPATCH_WAIT_EXISTING room=%s reason=already_in_progress", room_name)
        deadline = time.monotonic() + max(float(SELFHOST_DISPATCH_TIMEOUT_SECONDS), 30.0)
        while time.monotonic() < deadline:
            with _dispatching_rooms_lock:
                still_dispatching = room_key in _dispatching_rooms
            if not still_dispatching:
                logger.info("LIVEKIT_DISPATCH_WAIT_EXISTING_DONE room=%s action=reuse", room_name)
                return await _ensure_agent_dispatch_inner(
                    room_name,
                    credential,
                    recreate_existing=recreate_existing,
                )
            await asyncio.sleep(0.05)
        logger.warning("LIVEKIT_DISPATCH_WAIT_EXISTING_TIMEOUT room=%s", room_name)
        return None
    logger.info("LIVEKIT_DISPATCH_LOCK_ACQUIRED room=%s", room_name)
    try:
        return await _ensure_agent_dispatch_inner(room_name, credential, recreate_existing=recreate_existing)
    finally:
        with _dispatching_rooms_lock:
            _dispatching_rooms.discard(room_key)
        logger.info("LIVEKIT_DISPATCH_LOCK_RELEASED room=%s", room_name)


async def _ensure_livekit_room_only(room_name: str, credential: LiveKitCredential) -> bool:
    from livekit.api import CreateRoomRequest, LiveKitAPI

    try:
        async with LiveKitAPI(
            url=credential.url,
            api_key=credential.api_key,
            api_secret=credential.api_secret,
        ) as livekit_api:
            await livekit_api.room.create_room(CreateRoomRequest(name=room_name))
        logger.info("SESSION_PREPARE_ROOM_OK room=%s", room_name)
        return True
    except Exception as exc:
        # LiveKit returns an error when the room already exists; for prepare this is OK.
        logger.info("SESSION_PREPARE_ROOM_OK room=%s reason=%s", room_name, exc)
        return True


async def _ensure_agent_dispatch_inner(
    room_name: str,
    credential: "LiveKitCredential",
    *,
    recreate_existing: bool = False,
) -> dict | None:
    from livekit.api import CreateAgentDispatchRequest, CreateRoomRequest, LiveKitAPI
    import asyncio
    logger.info("[LIVEKIT_DISPATCH_REQUEST] room=%s agent_name=%s attempt=%s", room_name, AGENT_NAME or "<missing>", 1)
    logger.info(
            "[SESSION_START][DISPATCH] ensuring room=%s slot=%s agent_name=%s",
            room_name,
            credential.slot,
            AGENT_NAME or "<missing>",
        )
    last_error = None
    # Garantir que a room existe no LiveKit ANTES de tentar dispatch.
    # Se a room não existe, create_dispatch retorna 503 (não "room not found"),
    # então criamos proativamente em vez de esperar o erro.
    try:
        async with LiveKitAPI(
            url=credential.url,
            api_key=credential.api_key,
            api_secret=credential.api_secret,
        ) as _room_ensure_api:
            await _room_ensure_api.room.create_room(CreateRoomRequest(name=room_name))
            logger.info("[LIVEKIT_ROOM_ENSURE_OK] room=%s", room_name)
    except Exception as _room_exc:
        logger.info("[LIVEKIT_ROOM_ENSURE_SKIP] room=%s reason=%s", room_name, _room_exc)
    for attempt in range(1, 4):
            try:
                logger.info("BOOT_REAL_DISPATCH_BEGIN room=%s agent_name=%s attempt=%s", room_name, AGENT_NAME or "<missing>", attempt)
                logger.info("[LIVEKIT_DISPATCH_REQUEST] room=%s agent_name=%s attempt=%s", room_name, AGENT_NAME or "<missing>", attempt)
                logger.info("[LIVEKIT_DISPATCH_LIST_REQUEST] room=%s", room_name)
                logger.info(
                    "[SESSION_START][DISPATCH] attempt=%s room=%s slot=%s",
                    attempt,
                    room_name,
                    credential.slot,
                )
                async with LiveKitAPI(
                    url=credential.url,
                    api_key=credential.api_key,
                    api_secret=credential.api_secret,
                ) as livekit_api:
                    dispatches = await livekit_api.agent_dispatch.list_dispatch(room_name)
                    logger.info("[LIVEKIT_DISPATCH_LIST_OK] count=%s", len(dispatches))
                    for dispatch in dispatches:
                        if dispatch.agent_name == AGENT_NAME:
                            if recreate_existing:
                                # Nunca deletar dispatch ativo durante sessao normal.
                                # recreate_existing=True é ignorado para proteger o job em execucao.
                                logger.warning(
                                    "LIVEKIT_DISPATCH_RECREATE_IGNORED_DURING_ACTIVE_SESSION room=%s dispatch_id=%s",
                                    room_name,
                                    dispatch.id,
                                )
                            logger.info(
                                "LIVEKIT_DISPATCH_EXISTING_FOUND room=%s dispatch_id=%s",
                                room_name,
                                dispatch.id,
                            )
                            logger.info(
                                "LIVEKIT_DISPATCH_ALREADY_ACTIVE room=%s dispatch_id=%s action=return_existing",
                                room_name,
                                dispatch.id,
                            )
                            logger.info("BOOT_REAL_DISPATCH_OK room=%s dispatch_id=%s created=false", room_name, dispatch.id)
                            return {"id": dispatch.id, "created": False, "agent_name": AGENT_NAME}

                    logger.info(
                        "LIVEKIT_DISPATCH_CREATE_BEGIN room=%s agent_name=%s strategy=explicit_dispatch",
                        room_name,
                        AGENT_NAME or "<missing>",
                    )
                    created = await livekit_api.agent_dispatch.create_dispatch(
                        CreateAgentDispatchRequest(
                            agent_name=AGENT_NAME,
                            room=room_name,
                        )
                    )
                    logger.info(
                        "LIVEKIT_DISPATCH_CREATE_OK room=%s agent_name=%s dispatch_id=%s",
                        room_name,
                        AGENT_NAME or "<missing>",
                        created.id,
                    )
                    logger.info(
                        "[SESSION_START][DISPATCH] created_dispatch room=%s dispatch_id=%s",
                        room_name,
                        created.id,
                    )
                    logger.info("BOOT_REAL_DISPATCH_OK room=%s dispatch_id=%s created=true", room_name, created.id)
                    return {"id": created.id, "created": True, "agent_name": AGENT_NAME}
            except Exception as exc:
                err = str(exc)
                logger.error(
                    "BOOT_REAL_SESSION_FAILED reason=dispatch_error room=%s attempt=%s error=%s",
                    room_name,
                    attempt,
                    err,
                )
                if "requested room does not exist" in err.lower():
                    logger.info("[LIVEKIT_ROOM_CREATE_REQUEST] room=%s", room_name)
                    try:
                        async with LiveKitAPI(
                            url=credential.url,
                            api_key=credential.api_key,
                            api_secret=credential.api_secret,
                        ) as room_api:
                            await room_api.room.create_room(CreateRoomRequest(name=room_name))
                        logger.info("[LIVEKIT_ROOM_CREATE_OK] room=%s", room_name)
                        logger.info(
                            "[SESSION_START][DISPATCH] room_created room=%s slot=%s",
                            room_name,
                            credential.slot,
                        )
                        _runtime_log("AGENT_CONNECTING", stage="room_created", room=room_name)
                        continue
                    except Exception as room_exc:
                        logger.warning("[LIVEKIT_ROOM_CREATE_FAILED] room=%s status=unknown error=%s", room_name, room_exc)
                last_error = exc
                logger.warning("[LIVEKIT_DISPATCH_CREATE_FAILED] status=503 error=%s body=%s", exc, err)
                logger.warning(
                    "[LIVEKIT_DISPATCH_503_DIAGNOSIS] room_exists=unknown worker_ready=%s ui_room_connected=%s agent_name=%s",
                    str(bool(_runtime_state_snapshot().get("agent_worker_ready"))).lower(),
                    str(bool(_session_debug_snapshot().get("ui_room_connected"))).lower(),
                    AGENT_NAME or "<missing>",
                )
                logger.warning(
                    "[SESSION_START][DISPATCH] retry room=%s attempt=%s error=%s",
                    room_name,
                    attempt,
                    exc,
                )
                _runtime_log("AGENT_CONNECTING", stage="dispatch_retry", attempt=attempt)
                await asyncio.sleep(1.0)
    logger.error("[SESSION_START][DISPATCH] failed room=%s error=%s", room_name, last_error)
    raise RuntimeError(f"dispatch_unavailable:{last_error}")


async def _delete_and_refresh_dispatch(
    room_name: str,
    dispatch_id: str,
    credential: "LiveKitCredential",
    *,
    max_attempts: int | None = None,
) -> dict | None:
    """Deleta um dispatch cujo job nao foi entregue ao worker e cria um novo.

    Seguro apenas quando room_connected=False (agent nao entrou na room).
    Nunca chame se functional_ready=True ou room_connected=True.
    """
    from livekit.api import LiveKitAPI
    import asyncio

    with _dispatch_refresh_lock:
        attempts = _dispatch_refresh_attempts.get(room_name, 0)
        effective_max_attempts = max_attempts if isinstance(max_attempts, int) and max_attempts > 0 else KIRA_DISPATCH_REFRESH_MAX_ATTEMPTS
        if attempts >= effective_max_attempts:
            logger.warning(
                "LIVEKIT_DISPATCH_WORKER_UNAVAILABLE_FINAL room=%s attempts=%s reason=max_attempts_reached limit=%s",
                room_name, attempts, effective_max_attempts,
            )
            logger.warning("SESSION_DISPATCH_REFRESH_LIMIT_REACHED room=%s", room_name)
            return None
        _dispatch_refresh_attempts[room_name] = attempts + 1

    logger.warning(
        "LIVEKIT_DISPATCH_WORKER_UNAVAILABLE_RETRY room=%s attempt=%s dispatch_id=%s",
        room_name, attempts + 1, dispatch_id,
    )
    try:
        async with LiveKitAPI(
            url=credential.url,
            api_key=credential.api_key,
            api_secret=credential.api_secret,
        ) as lk:
            await lk.agent_dispatch.delete_dispatch(dispatch_id, room_name)
            logger.info(
                "LIVEKIT_DISPATCH_DELETED_FOR_REFRESH room=%s dispatch_id=%s",
                room_name, dispatch_id,
            )
    except Exception as del_exc:
        logger.warning(
            "LIVEKIT_DISPATCH_DELETE_FAILED room=%s dispatch_id=%s error=%s",
            room_name, dispatch_id, del_exc,
        )

    await asyncio.sleep(KIRA_SESSION_AGENT_JOIN_WAIT_SECONDS)
    try:
        new_dispatch = await _ensure_agent_dispatch(room_name, credential)
        logger.info(
            "SESSION_AGENT_NOT_JOINED_DISPATCH_REFRESH room=%s new_dispatch_id=%s",
            room_name, (new_dispatch or {}).get("id", "none"),
        )
        logger.info(
            "SESSION_AGENT_JOIN_RECOVERY_DISPATCHED new_dispatch_id=%s",
            (new_dispatch or {}).get("id", "none"),
        )
        return new_dispatch
    except Exception as new_exc:
        logger.warning(
            "SESSION_AGENT_JOIN_RETRY_FAILED room=%s error=%s",
            room_name, new_exc,
        )
        return None


async def _prepare_selfhost_room_for_agent(
    room_name: str,
    credential: LiveKitCredential,
    *,
    room_config_supported: bool,
) -> dict[str, object]:
    result: dict[str, object] = {
        "selfhostDispatchMode": "room_config_fallback" if room_config_supported else "dispatch_unavailable",
        "dispatchInfo": None,
        "roomPrepared": bool(room_config_supported),
        "roomConfigSupported": bool(room_config_supported),
    }
    _runtime_log("SELFHOST_DISPATCH_ATTEMPT", room=room_name, slot=credential.slot)
    logger.info(
        "[SESSION_START][SELFHOST] preparing room=%s slot=%s room_config_supported=%s",
        room_name,
        credential.slot,
        room_config_supported,
    )
    try:
        dispatch_info = await asyncio.wait_for(
            _ensure_agent_dispatch(room_name, credential),
            timeout=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
        )
        result.update(
            {
                "selfhostDispatchMode": "explicit_dispatch",
                "dispatchInfo": dispatch_info,
                "roomPrepared": True,
            }
        )
        _runtime_log(
            "SELFHOST_DISPATCH_OK",
            room=room_name,
            dispatch_id=str((dispatch_info or {}).get("id") or ""),
            created=str(bool((dispatch_info or {}).get("created"))).lower(),
        )
        logger.info(
            "[SESSION_START][SELFHOST] dispatch_ready room=%s dispatch_id=%s created=%s",
            room_name,
            str((dispatch_info or {}).get("id") or ""),
            bool((dispatch_info or {}).get("created")),
        )
        return result
    except asyncio.TimeoutError:
        result["dispatchError"] = "dispatch_timeout"
        _runtime_log("SELFHOST_DISPATCH_UNSUPPORTED", room=room_name, reason="timeout")
        logger.warning("[SESSION_START][SELFHOST] dispatch_timeout room=%s", room_name)
    except Exception as exc:
        result["dispatchError"] = str(exc)
        if room_config_supported:
            _runtime_log("SELFHOST_DISPATCH_FAILED", room=room_name, error=str(exc))
        else:
            _runtime_log("SELFHOST_DISPATCH_UNSUPPORTED", room=room_name, reason=str(exc))
        logger.warning("[SESSION_START][SELFHOST] dispatch_error room=%s error=%s", room_name, exc)
    return result


def _dispatch_agent_for_room_background(room_name: str, *, reason: str = "ui_room_connected") -> None:
    normalized_room = str(room_name or "").strip()
    if not normalized_room:
        return

    def _runner() -> None:
        dispatch_start = time.perf_counter()
        try:
            credential = _resolve_livekit_credential_for_room(normalized_room)
            _session_start_log("SESSION_START_DISPATCH_BEGIN", room=normalized_room, reason=reason)
            logger.info("BOOT_REAL_DISPATCH_BEGIN room=%s reason=%s", normalized_room, reason)
            dispatch_info = asyncio.run(
                asyncio.wait_for(
                    _ensure_agent_dispatch(normalized_room, credential),
                    timeout=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
                )
            )
            dispatch_id = str((dispatch_info or {}).get("id") or "")
            created = bool((dispatch_info or {}).get("created"))
            _update_session_debug(
                dispatch_info=dispatch_info,
                room_prepared=True,
                last_event="SESSION_DISPATCH_BACKGROUND_OK",
                last_message="Dispatch do agente criado em background.",
                last_error="",
                last_stage="dispatch_background",
            )
            _session_start_log(
                "SESSION_START_DISPATCH_DONE",
                room=normalized_room,
                reason=reason,
                dispatch_id=dispatch_id or "none",
                created=str(created).lower(),
                elapsed_ms=_elapsed_ms(dispatch_start),
            )
            logger.info(
                "BOOT_REAL_DISPATCH_OK room=%s dispatch_id=%s created=%s",
                normalized_room,
                dispatch_id or "none",
                str(created).lower(),
            )
            if dispatch_info and created:
                _session_start_log(
                    "SESSION_DISPATCH_CREATED",
                    room=normalized_room,
                    dispatch_id=dispatch_id or "none",
                    reason=reason,
                )
            elif dispatch_info:
                _session_start_log(
                    "SESSION_DISPATCH_ALREADY_EXISTS",
                    room=normalized_room,
                    dispatch_id=dispatch_id or "none",
                    reason=reason,
                )
                join_wait_seconds = max(KIRA_SESSION_AGENT_JOIN_WAIT_SECONDS, 25.0)
                time.sleep(join_wait_seconds)
                runtime_snapshot = _runtime_state_snapshot()
                room_connected = bool(
                    runtime_snapshot.get("room_connected")
                    or runtime_snapshot.get("room_ready")
                    or runtime_snapshot.get("agent_connected")
                )
                if dispatch_id and not room_connected:
                    _session_start_log(
                        "SESSION_DISPATCH_REFRESH_BEGIN",
                        room=normalized_room,
                        dispatch_id=dispatch_id,
                        reason="existing_dispatch_agent_not_joined_after_grace",
                        grace_seconds=join_wait_seconds,
                    )
                    refreshed = asyncio.run(
                        _delete_and_refresh_dispatch(normalized_room, dispatch_id, credential)
                    )
                    refreshed_id = str((refreshed or {}).get("id") or "")
                    _update_session_debug(
                        dispatch_info=refreshed or dispatch_info,
                        room_prepared=True,
                        last_event="SESSION_DISPATCH_REFRESHED",
                        last_message="Dispatch travado recriado em background.",
                        last_error="",
                        last_stage="dispatch_refresh",
                    )
                    _session_start_log(
                        "SESSION_DISPATCH_REFRESHED",
                        room=normalized_room,
                        old_dispatch_id=dispatch_id,
                        new_dispatch_id=refreshed_id or "none",
                        reason="existing_dispatch_agent_not_joined_after_grace",
                    )
                elif dispatch_id:
                    _session_start_log(
                        "SESSION_DISPATCH_REFRESH_SKIPPED",
                        room=normalized_room,
                        dispatch_id=dispatch_id,
                        reason="agent_joined_during_grace",
                        grace_seconds=join_wait_seconds,
                    )
            else:
                _session_start_log(
                    "SESSION_DISPATCH_SKIPPED",
                    room=normalized_room,
                    reason="already_in_progress_or_unavailable",
                )
        except asyncio.TimeoutError:
            logger.error(
                "BOOT_REAL_SESSION_FAILED reason=dispatch_timeout room=%s timeout_s=%s",
                normalized_room,
                SELFHOST_DISPATCH_TIMEOUT_SECONDS,
            )
            elapsed = _elapsed_ms(dispatch_start)
            _session_start_log(
                "LIVEKIT_DISPATCH_TIMEOUT",
                level="error",
                room=normalized_room,
                reason=reason,
                elapsed_ms=elapsed,
                timeout_s=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
            )
            _session_start_log(
                "SESSION_START_TIMEOUT",
                level="error",
                stage="dispatch",
                room=normalized_room,
                elapsed_ms=elapsed,
                timeout_s=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
            )
            _update_session_debug(
                last_event="SESSION_DISPATCH_BACKGROUND_TIMEOUT",
                last_error="Timeout criando dispatch do agente em background.",
                last_stage="dispatch",
                last_dispatch_error_code="timeout",
            )
        except Exception as exc:
            logger.error(
                "BOOT_REAL_SESSION_FAILED reason=dispatch_background_error room=%s error=%s",
                normalized_room,
                exc,
            )
            _session_start_log(
                "SESSION_DISPATCH_FAILED",
                level="error",
                room=normalized_room,
                reason=reason,
                error=str(exc),
            )
            _update_session_debug(
                last_event="SESSION_DISPATCH_BACKGROUND_FAILED",
                last_error=str(exc),
                last_stage="dispatch",
                last_dispatch_error_code=exc.__class__.__name__,
            )

    threading.Thread(
        target=_runner,
        name=f"kira-dispatch-{normalized_room[:24]}",
        daemon=True,
    ).start()


async def _probe_livekit_worker_credential(credential: LiveKitCredential) -> None:
    try:
        from livekit.api import AccessToken, VideoGrants
        import aiohttp
    except Exception as exc:
        raise RuntimeError("Pacote LiveKit indisponivel para validar credencial do worker.") from exc

    grant = VideoGrants(agent=True)
    join_jwt = (
        AccessToken(api_key=credential.api_key, api_secret=credential.api_secret)
        .with_grants(grant)
        .to_jwt()
    )

    parsed = urlsplit(credential.url)
    scheme = parsed.scheme or "wss"
    if scheme.startswith("http"):
        scheme = scheme.replace("http", "ws", 1)
    base = f"{scheme}://{parsed.netloc}{parsed.path}".rstrip("/") + "/"
    agent_url = urlunsplit((scheme, parsed.netloc, (parsed.path.rstrip("/") + "/agent") or "/agent", "", ""))
    if not parsed.path:
        agent_url = f"{base}agent"

    timeout = aiohttp.ClientTimeout(total=5, connect=4, sock_connect=4, sock_read=4)
    headers = {"Authorization": f"Bearer {join_jwt}"}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        ws = await session.ws_connect(
            agent_url,
            headers=headers,
            autoping=True,
        )
        await ws.close()
        # O agente conecta mesmo com os minutos grátis do mês esgotados, mas a tela é recusada ("connection minutes
        # limit exceeded" -> erro de websocket na tela). Pergunta como a tela entraria e pula a conta se recusar.
        join = (
            AccessToken(api_key=credential.api_key, api_secret=credential.api_secret)
            .with_identity("kira-probe")
            .with_grants(VideoGrants(room_join=True, room="kira_probe"))
            .to_jwt()
        )
        http_base = f"{'https' if scheme == 'wss' else 'http'}://{parsed.netloc}{parsed.path.rstrip('/')}"
        async with session.get(f"{http_base}/rtc/validate", params={"access_token": join}) as resp:
            if resp.status in (401, 403, 429):
                detail = (await resp.text())[:160]
                logger.warning("LIVEKIT_SLOT_REJECTS_CLIENTS slot=%s status=%s detail=%s", credential.slot, resp.status, detail)
                raise RuntimeError(f"conta LiveKit recusa a tela: {detail}")


def _select_bootstrap_livekit_credential() -> LiveKitCredential | None:
    last_error: Exception | None = None

    for credential in _ordered_livekit_credentials(exclude_failed_slots=True):
        logger.info(
            "[AGENT_BOOT] probing LiveKit credential slot=%s total_slots=%s url=%s",
            credential.slot,
            LIVEKIT_MANAGER.total_slots,
            credential.url,
        )
        LIVEKIT_MANAGER.mark_current(credential.slot)
        _runtime_log("LIVEKIT_TRY_SLOT", slot=credential.slot)
        _update_livekit_runtime_state(
            livekit_current_slot=credential.slot,
            livekit_total_slots=LIVEKIT_MANAGER.total_slots,
            livekit_status="starting",
            livekit_worker_boot_status="probing",
            voice_mode="voice",
            restart_required_for_voice=False,
            livekit_message=_livekit_runtime_message_for_status("starting"),
            log_reason="bootstrap_probe",
        )
        logger.info("[LIVEKIT_BOOTSTRAP_PROBE_START] slot=%s", credential.slot)
        try:
            # LiveKit local às vezes demora a responder logo após derrubar o agente antigo: tenta de novo.
            _is_local_lk = any(h in str(credential.url or "") for h in ("127.0.0.1", "localhost"))
            for _attempt in range(3 if _is_local_lk else 1):
                try:
                    asyncio.run(_probe_livekit_worker_credential(credential))
                    break
                except Exception as _probe_exc:
                    if _attempt >= (2 if _is_local_lk else 0) or is_livekit_auth_error(_probe_exc):
                        raise
                    logger.warning("LIVEKIT_LOCAL_PROBE_RETRY slot=%s attempt=%s error=%s", credential.slot, _attempt + 1, _probe_exc)
                    time.sleep(2.0)
            _update_livekit_runtime_state(
                livekit_current_slot=credential.slot,
                livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                livekit_status="ready",
                livekit_worker_boot_status="ready",
                livekit_ready_source="bootstrap_probe_ok",
                voice_mode="voice",
                restart_required_for_voice=False,
                livekit_message=_livekit_runtime_message_for_status("ready"),
                log_reason="bootstrap_probe_ok",
            )
            logger.info("[LIVEKIT_BOOTSTRAP_PROBE_OK] slot=%s livekit_status=ready", credential.slot)
            return credential
        except Exception as exc:
            last_error = exc
            if is_livekit_auth_error(exc):
                _mark_livekit_failed_slot(credential.slot, reason="auth_failed")
                logger.warning("LIVEKIT_SLOT_AUTH_FAILED_CONTINUE slot=%s error=%s", credential.slot, exc)
                _runtime_log("LIVEKIT_SLOT_AUTH_FAILED_CONTINUE", slot=credential.slot)
                _update_livekit_runtime_state(
                    livekit_current_slot=credential.slot,
                    livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                    livekit_status="auth_failed",
                    voice_mode="voice",
                    restart_required_for_voice=False,
                    livekit_message="Credenciais LiveKit invalidas. Tentando proximo slot.",
                    log_reason="bootstrap_auth_failed",
                )
                continue
            if is_livekit_quota_error(exc):
                _mark_livekit_failed_slot(credential.slot, reason="quota_exceeded")
                logger.warning("LIVEKIT_SLOT_QUOTA_FAILED_CONTINUE slot=%s error=%s", credential.slot, exc)
                _runtime_log("LIVEKIT_SLOT_QUOTA_FAILED_CONTINUE", slot=credential.slot)
                _update_livekit_runtime_state(
                    livekit_current_slot=credential.slot,
                    livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                    livekit_status="quota_exceeded",
                    voice_mode="voice",
                    restart_required_for_voice=False,
                    livekit_message="LiveKit sem cota neste slot. Tentando outra credencial LiveKit.",
                    log_reason="bootstrap_quota_failed",
                )
                continue
            logger.exception("LIVEKIT_SLOT_GENERIC_FAILED_CONTINUE slot=%s", credential.slot)
            _mark_livekit_failed_slot(credential.slot, reason="failed")
            continue

    _runtime_log("LIVEKIT_ALL_SLOTS_FAILED")
    _update_livekit_runtime_state(
        livekit_current_slot=LIVEKIT_MANAGER.current_slot,
        livekit_total_slots=LIVEKIT_MANAGER.total_slots,
        livekit_status="all_failed",
        voice_mode="unavailable",
        restart_required_for_voice=True,
        livekit_message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
        log_reason="bootstrap_all_failed",
    )
    _update_runtime_readiness(
        ui_status="backend_ready",
        event="LIVEKIT_ALL_FAILED",
        error="",
        failure_reason="",
        message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
    )
    _publish_backend_ready_if_possible()
    logger.error(
        "[AGENT_BOOT] Nenhuma credencial LiveKit valida para iniciar o worker: %s",
        last_error,
    )
    return None


def _livekit_runtime_message_for_status(status: str) -> str:
    normalized = str(status or "unknown").strip().lower() or "unknown"
    if normalized == "ready":
        return "LiveKit pronto."
    if normalized == "local_ready":
        return "LiveKit local pronto."
    if normalized == "worker_booting":
        return "LiveKit pronto; iniciando worker."
    if normalized == "ok":
        return "LiveKit OK"
    if normalized == "auth_failed":
        return "Credenciais LiveKit invalidas no slot atual. Tentando proxima credencial LiveKit."
    if normalized == "quota_exceeded":
        return "LiveKit sem cota. Voz pausada. Reinicie a sessao para tentar outra credencial LiveKit."
    if normalized == "all_failed":
        return "LiveKit indisponivel. Nao foi possivel preparar o token da sessao."
    if normalized == "starting":
        return "Tentando credenciais LiveKit..."
    return "LiveKit aguardando sessao."


async def _create_livekit_session_payload(
    *,
    room_name: str,
    participant_name: str,
    create_dispatch: bool = False,
) -> dict[str, object]:
    if not LIVEKIT_MANAGER.has_credentials():
        raise RuntimeError("Variaveis LiveKit nao encontradas no .env.")

    try:
        from livekit.api import AccessToken, VideoGrants
    except Exception as exc:
        raise RuntimeError("Pacote livekit-api indisponivel.") from exc

    participant_id = f"user_{random.randint(1000, 9999)}"
    grant = VideoGrants(
        room=room_name,
        room_join=True,
        can_publish=True,
        can_publish_data=True,
        can_subscribe=True,
    )
    room_config_supported = False
    RoomAgentDispatch = None
    RoomConfiguration = None
    if LIVEKIT_MANAGER.total_slots == 1 and AGENT_NAME:
        try:
            from livekit.protocol.agent_dispatch import RoomAgentDispatch as _RoomAgentDispatch
            from livekit.protocol.room import RoomConfiguration as _RoomConfiguration

            RoomAgentDispatch = _RoomAgentDispatch
            RoomConfiguration = _RoomConfiguration
            room_config_supported = True
        except Exception:
            logger.exception("LIVEKIT_ROOM_CONFIG_DISPATCH_UNAVAILABLE room=%s", room_name)
    last_error: Exception | None = None

    for credential in _ordered_livekit_credentials(exclude_failed_slots=True):
        LIVEKIT_MANAGER.mark_current(credential.slot)
        _runtime_log("LIVEKIT_TRY_SLOT", slot=credential.slot)
        _update_livekit_runtime_state(
            livekit_current_slot=credential.slot,
            livekit_total_slots=LIVEKIT_MANAGER.total_slots,
            livekit_status="starting",
            voice_mode="voice",
            restart_required_for_voice=False,
            livekit_message=_livekit_runtime_message_for_status("starting"),
            log_reason="startup_try",
        )
        try:
            _runtime_log("SESSION_ROOM_NAME_CREATED", room=room_name, slot=credential.slot)
            if not create_dispatch:
                await _ensure_livekit_room_only(room_name, credential)
            token_start = time.perf_counter()
            _session_start_log(
                "SESSION_START_TOKEN_BEGIN",
                room=room_name,
                participant=participant_name,
                slot=credential.slot,
            )
            token_builder = (
                AccessToken(api_key=credential.api_key, api_secret=credential.api_secret)
                .with_identity(participant_id)
                .with_name(participant_name)
                # TTL longo: o token preparado no boot deve continuar valido pra
                # conectar mesmo que o usuario demore a clicar INICIAR SESSAO.
                # (antes 15min; a janela de frescor da Kira era 60s, ainda menor.)
                .with_ttl(timedelta(hours=6))
                .with_grants(grant)
            )
            dispatch_info = None
            selfhost_dispatch_mode = "cloud_dispatch"
            room_prepared = False
            use_room_config_dispatch = bool(
                create_dispatch
                and
                LIVEKIT_MANAGER.total_slots == 1
                and AGENT_NAME
                and room_config_supported
                and RoomAgentDispatch is not None
                and RoomConfiguration is not None
                and LIVEKIT_DISPATCH_STRATEGY in {"auto", "room_config", "both"}
            )
            if LIVEKIT_MANAGER.total_slots == 1:
                room_prepare_start = time.perf_counter()
                _session_start_log(
                    "SESSION_START_ROOM_PREPARE_BEGIN",
                    room=room_name,
                    mode="room_config" if use_room_config_dispatch else "explicit_dispatch",
                )
                if use_room_config_dispatch:
                    token_builder = token_builder.with_room_config(
                        RoomConfiguration(
                            agents=[RoomAgentDispatch(agent_name=AGENT_NAME)]
                        )
                    )
                    selfhost_dispatch_mode = "room_config"
                    room_prepared = True
                    _runtime_log("SELFHOST_ROOM_CONFIG_ATTACHED", value="true", room=room_name, mode=selfhost_dispatch_mode)
                    _session_start_log(
                        "SESSION_START_ROOM_PREPARE_DONE",
                        room=room_name,
                        mode=selfhost_dispatch_mode,
                        elapsed_ms=_elapsed_ms(room_prepare_start),
                    )
                    _session_start_log(
                        "SESSION_DISPATCH_SKIPPED",
                        room=room_name,
                        reason="room_config_attached",
                    )
                else:
                    _session_start_log(
                        "SESSION_START_ROOM_PREPARE_DONE",
                        room=room_name,
                        mode="explicit_dispatch",
                        elapsed_ms=_elapsed_ms(room_prepare_start),
                    )
            token = token_builder.to_jwt()
            token_created_at = time.time()
            _session_start_log(
                "SESSION_START_TOKEN_DONE",
                room=room_name,
                participant=participant_name,
                slot=credential.slot,
                elapsed_ms=_elapsed_ms(token_start),
            )
            should_run_explicit_dispatch = bool(
                create_dispatch
                and
                AGENT_NAME
                and (
                    LIVEKIT_MANAGER.total_slots != 1
                    or (
                        LIVEKIT_MANAGER.total_slots == 1
                        and not use_room_config_dispatch
                        and LIVEKIT_DISPATCH_STRATEGY in {"auto", "explicit", "both"}
                    )
                )
            )
            if should_run_explicit_dispatch:
                dispatch_start = time.perf_counter()
                _session_start_log(
                    "SESSION_START_DISPATCH_BEGIN",
                    room=room_name,
                    mode="explicit_dispatch",
                    timeout_s=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
                )
                logger.info("LIVEKIT_ROOM_ENSURE_START room=%s", room_name)
                try:
                    dispatch_info = await asyncio.wait_for(
                        _ensure_agent_dispatch(room_name, credential),
                        timeout=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as dispatch_timeout:
                    elapsed = _elapsed_ms(dispatch_start)
                    _runtime_log("LIVEKIT_DISPATCH_TIMEOUT", room=room_name, timeout=SELFHOST_DISPATCH_TIMEOUT_SECONDS)
                    _session_start_log(
                        "SESSION_START_TIMEOUT",
                        level="error",
                        stage="dispatch",
                        room=room_name,
                        elapsed_ms=elapsed,
                        timeout_s=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
                    )
                    _update_session_debug(
                        requested_room_name=room_name,
                        token_issued=True,
                        room_prepared=False,
                        last_event="DISPATCH_TIMEOUT",
                        last_message="Timeout ao preparar dispatch LiveKit.",
                        last_error=f"dispatch_timeout:{SELFHOST_DISPATCH_TIMEOUT_SECONDS}",
                        last_stage="dispatch_failed",
                    )
                    raise SessionStartStageTimeout(
                        "dispatch",
                        "Timeout ao preparar dispatch LiveKit.",
                        timeout_s=SELFHOST_DISPATCH_TIMEOUT_SECONDS,
                        elapsed_ms=elapsed,
                        details={"room": room_name},
                    ) from dispatch_timeout
                except Exception as dispatch_exc:
                    elapsed = _elapsed_ms(dispatch_start)
                    _session_start_log(
                        "SESSION_DISPATCH_FAILED",
                        level="error",
                        room=room_name,
                        elapsed_ms=elapsed,
                        error=str(dispatch_exc),
                    )
                    raise SessionStartStageError(
                        "dispatch",
                        f"Falha ao preparar dispatch LiveKit: {dispatch_exc}",
                        status_code=502,
                        error_code="dispatch_failed",
                        details={"room": room_name, "elapsed_ms": elapsed},
                    ) from dispatch_exc
                selfhost_dispatch_mode = "explicit_dispatch" if LIVEKIT_MANAGER.total_slots == 1 else "cloud_dispatch"
                room_prepared = True
                logger.info("LIVEKIT_DISPATCH_CREATE_OK room=%s dispatch_id=%s", room_name, str((dispatch_info or {}).get("id") or ""))
                _session_start_log(
                    "SESSION_START_DISPATCH_DONE",
                    room=room_name,
                    mode=selfhost_dispatch_mode,
                    elapsed_ms=_elapsed_ms(dispatch_start),
                )
            _update_session_debug(
                requested_room_name=room_name,
                selfhost_dispatch_mode=selfhost_dispatch_mode,
                dispatch_info=dispatch_info,
                room_prepared=True,
                token_ready=True,
            )
            _runtime_log(
                "LIVEKIT_TOKEN_CREATED",
                room=room_name,
                slot=credential.slot,
                selfhost_dispatch_mode=selfhost_dispatch_mode,
            )
            provider = _selected_livekit_provider()
            logger.info("LIVEKIT_TOKEN_PROVIDER_USED name=%s kind=%s url=%s", provider.name, provider.kind, provider.url)
            logger.info(
                "[SESSION_START] token_created room=%s participant=%s dispatch_mode=%s room_prepared=%s",
                room_name,
                participant_name,
                selfhost_dispatch_mode,
                True,
            )
            LIVEKIT_MANAGER.save_last_good_slot(credential.slot)
            _runtime_log("LIVEKIT_SLOT_OK", slot=credential.slot)
            _update_livekit_runtime_state(
                livekit_current_slot=credential.slot,
                livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                livekit_status="ok",
                voice_mode="voice",
                restart_required_for_voice=False,
                livekit_message=_livekit_runtime_message_for_status("ok"),
                log_reason="startup_ok",
            )
            _update_session_debug(
                requested_room_name=room_name,
                token_issued=True,
                selfhost_dispatch_mode=selfhost_dispatch_mode,
                dispatch_info=dispatch_info,
                room_prepared=True,
                token_ready=True,
                token_created_at=time.time(),
                token_expires_at=time.time() + KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS,
                last_event="LIVEKIT_TOKEN_CREATED",
                last_message="Token criado. Aguardando conexao da UI e do agente na room.",
                last_error="",
                last_stage="token_created",
            )
            extra = {"agentName": AGENT_NAME} if AGENT_NAME else {}
            if dispatch_info:
                extra["agentDispatch"] = dispatch_info
            extra["selfhostDispatchMode"] = selfhost_dispatch_mode
            extra["dispatchInfo"] = dispatch_info
            extra["roomPrepared"] = True
            extra["livekitProvider"] = provider.name
            extra["livekitProviderKind"] = provider.kind
            extra["livekit_provider"] = {
                "name": provider.name,
                "kind": provider.kind,
            }
            return {
                "serverUrl": credential.url,
                "roomName": room_name,
                "participantName": participant_name,
                "participantIdentity": participant_id,
                "participantToken": token,
                "url": credential.url,
                "room_name": room_name,
                "participant_name": participant_name,
                "participant_identity": participant_id,
                "token": token,
                "token_ready": True,
                "tokenReady": True,
                "room_prepared": True,
                "token_created_at": token_created_at,
                "token_expires_at": token_created_at + KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS,
                **extra,
            }
        except Exception as exc:
            if isinstance(exc, SessionStartStageError):
                raise
            last_error = exc
            if is_livekit_quota_error(exc):
                _mark_livekit_failed_slot(credential.slot, reason="quota_exceeded")
                logger.warning("LIVEKIT_QUOTA_EXCEEDED_ON_STARTUP slot=%s error=%s", credential.slot, exc)
                _runtime_log("LIVEKIT_QUOTA_EXCEEDED_ON_STARTUP", slot=credential.slot)
                _update_livekit_runtime_state(
                    livekit_current_slot=credential.slot,
                    livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                    livekit_status="quota_exceeded",
                    voice_mode="voice",
                    restart_required_for_voice=False,
                    livekit_message="LiveKit sem cota neste slot. Tentando outra credencial LiveKit.",
                    log_reason="startup_quota",
                )
                continue
            if is_livekit_auth_error(exc):
                _mark_livekit_failed_slot(credential.slot, reason="auth_failed")
                logger.warning("LIVEKIT_AUTH_FAILED slot=%s error=%s", credential.slot, exc)
                _runtime_log("LIVEKIT_AUTH_FAILED", slot=credential.slot)
                _update_livekit_runtime_state(
                    livekit_current_slot=credential.slot,
                    livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                    livekit_status="auth_failed",
                    voice_mode="voice",
                    restart_required_for_voice=False,
                    livekit_message=_livekit_runtime_message_for_status("auth_failed"),
                    log_reason="startup_auth_failed",
                )
                continue
            _mark_livekit_failed_slot(credential.slot, reason="failed")
            logger.exception("LIVEKIT_SLOT_FAILED slot=%s", credential.slot)
            continue

    _update_livekit_runtime_state(
        livekit_current_slot=LIVEKIT_MANAGER.current_slot,
        livekit_total_slots=LIVEKIT_MANAGER.total_slots,
        livekit_status="all_failed",
        voice_mode="unavailable",
        restart_required_for_voice=True,
        livekit_message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
        log_reason="startup_all_failed",
    )
    _update_runtime_readiness(
        ui_status="backend_ready",
        event="LIVEKIT_ALL_FAILED",
        error="",
        failure_reason="",
        message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
    )
    _publish_backend_ready_if_possible()
    raise RuntimeError(f"Todas as credenciais LiveKit falharam: {last_error}")


async def _create_livekit_session_payload_with_timeout(
    *,
    room_name: str,
    participant_name: str,
    create_dispatch: bool = False,
) -> dict[str, object]:
    request_start = time.perf_counter()
    try:
        logger.info(
            "[SESSION_START] token_request_started room=%s participant=%s timeout_s=%.1f",
            room_name,
            participant_name,
            LIVEKIT_TOKEN_TIMEOUT_SECONDS,
        )
        payload = await _create_livekit_session_payload(
            room_name=room_name,
            participant_name=participant_name,
            create_dispatch=create_dispatch,
        )
        return payload
    except SessionStartStageTimeout as exc:
        _runtime_log("SESSION_START_FAILED", status=exc.status_code, stage=exc.stage, error=exc.error_code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error": exc.error_code,
                "message": str(exc),
                "room": room_name,
                "stage": exc.stage,
                **exc.details,
            },
        ) from exc
    except SessionStartStageError as exc:
        _runtime_log("SESSION_START_FAILED", status=exc.status_code, stage=exc.stage, error=exc.error_code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error": exc.error_code,
                "message": str(exc),
                "room": room_name,
                "stage": exc.stage,
                **exc.details,
            },
        ) from exc
    except asyncio.TimeoutError as exc:
        elapsed = _elapsed_ms(request_start)
        _runtime_log("SESSION_START_TIMEOUT", stage="unknown", room=room_name, elapsed_ms=elapsed, timeout=LIVEKIT_TOKEN_TIMEOUT_SECONDS)
        logger.error(
            "[SESSION_START] timeout_unknown room=%s participant=%s elapsed_ms=%s timeout_s=%.1f",
            room_name,
            participant_name,
            elapsed,
            LIVEKIT_TOKEN_TIMEOUT_SECONDS,
        )
        _update_session_debug(
            requested_room_name=room_name,
            token_issued=False,
            room_prepared=False,
            last_event="SESSION_START_TIMEOUT",
            last_message="Timeout ao preparar sessao.",
            last_error=f"unknown_timeout:{LIVEKIT_TOKEN_TIMEOUT_SECONDS}",
            last_stage="unknown_timeout",
        )
        raise HTTPException(
            status_code=504,
            detail={
                "error": "session_start_timeout",
                "message": "Timeout ao preparar sessao LiveKit.",
                "room": room_name,
                "stage": "unknown",
                "timeout": LIVEKIT_TOKEN_TIMEOUT_SECONDS,
            },
        ) from exc


def _session_identity_from_payload(payload: dict | None = None) -> tuple[str, str]:
    payload = payload or {}
    room_name = str(payload.get("room_name") or payload.get("roomName") or payload.get("room") or "").strip()
    participant_name = str(
        payload.get("participant_name")
        or payload.get("participantName")
        or payload.get("participant")
        or payload.get("identity")
        or ""
    ).strip()
    if not room_name:
        room_name = f"kira_session_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    if not participant_name:
        participant_name = "usuario"
    return room_name, participant_name


def _connection_details_response_payload(session_payload: dict[str, object]) -> dict[str, object]:
    token_created_at = float(session_payload.get("token_created_at") or time.time())
    token_expires_at = float(
        session_payload.get("token_expires_at")
        or session_payload.get("expiresAt")
        or (token_created_at + KIRA_PREPARED_TOKEN_MAX_AGE_SECONDS)
    )
    provider_name = str(session_payload.get("livekitProvider") or "").strip()
    provider_kind = str(session_payload.get("livekitProviderKind") or "").strip()
    dispatch_info = session_payload.get("dispatchInfo") or session_payload.get("agentDispatch")
    normalized_dispatch = dict(dispatch_info) if isinstance(dispatch_info, dict) else None
    return {
        **session_payload,
        "ok": True,
        "serverUrl": session_payload.get("serverUrl") or session_payload.get("url"),
        "roomName": session_payload.get("roomName") or session_payload.get("room_name"),
        "participantName": session_payload.get("participantName") or session_payload.get("participant_name"),
        "participantIdentity": session_payload.get("participantIdentity") or session_payload.get("participant_identity"),
        "participantToken": session_payload.get("participantToken") or session_payload.get("token"),
        "livekitProvider": provider_name,
        "livekitProviderKind": provider_kind,
        "provider": {
            "name": provider_name,
            "kind": provider_kind,
        },
        "expiresAt": token_expires_at,
        "tokenCreatedAt": token_created_at,
        "tokenExpiresAt": token_expires_at,
        "session": {
            "roomName": session_payload.get("roomName") or session_payload.get("room_name"),
            "participantName": session_payload.get("participantName") or session_payload.get("participant_name"),
            "participantIdentity": session_payload.get("participantIdentity") or session_payload.get("participant_identity"),
            "createdAt": token_created_at,
            "expiresAt": token_expires_at,
        },
        "dispatchInfo": normalized_dispatch,
        "agentDispatch": normalized_dispatch,
        "selfhostDispatchMode": str(
            session_payload.get("selfhostDispatchMode")
            or ("explicit_dispatch" if normalized_dispatch else "prepared_no_dispatch")
        ),
    }


async def create_session_connection_details(payload: dict | None = None) -> dict[str, object]:
    room_name, participant_name = _session_identity_from_payload(payload)
    logger.info("SESSION_CONNECTION_DETAILS_BEGIN room=%s participant=%s", room_name, participant_name)
    provider = _selected_livekit_provider()
    logger.info(
        "SESSION_CONNECTION_DETAILS_PROVIDER_USED name=%s kind=%s room=%s",
        provider.name,
        provider.kind,
        room_name,
    )
    session_payload = await _create_livekit_session_payload_with_timeout(
        room_name=room_name,
        participant_name=participant_name,
        create_dispatch=False,
    )
    logger.info("SESSION_CONNECTION_DETAILS_ROOM_CREATED room=%s", room_name)
    logger.info("SESSION_CONNECTION_DETAILS_TOKEN_CREATED room=%s", room_name)
    prepared = _save_prepared_session(session_payload)
    details = _connection_details_response_payload(prepared)
    logger.info(
        "SESSION_CONNECTION_DETAILS_READY room=%s provider=%s kind=%s expires_at=%s",
        details.get("roomName") or room_name,
        details.get("livekitProvider") or provider.name,
        details.get("livekitProviderKind") or provider.kind,
        details.get("expiresAt") or "",
    )
    return details


def _can_attempt_session_start_with_current_state(state: dict[str, object] | None) -> bool:
    snapshot = state or {}
    if bool(snapshot.get("can_start_session")):
        return True

    livekit_status = str(snapshot.get("livekit_status") or "").strip().lower() or "unknown"
    if livekit_status in {"auth_failed", "quota_exceeded", "all_failed"}:
        return False

    if bool(snapshot.get("backend_ready")):
        return True
    if bool(snapshot.get("agent_connected")) or bool(snapshot.get("agent_process_spawned")):
        return True

    return livekit_status in {"starting", "unknown", "ok"}


def _agent_minimally_ready_for_session_start(state: dict[str, object] | None) -> tuple[bool, str]:
    snapshot = state or {}
    process_alive = bool(snapshot.get("agent_process_spawned")) and not bool(snapshot.get("agent_process_dead"))
    worker_ready = bool(
        snapshot.get("agent_worker_ready")
        or snapshot.get("worker_ready_received")
        or snapshot.get("agent_ready_phase") == "worker_ready"
        or snapshot.get("agent_probe_ok")
        or snapshot.get("agent_ready")
    )
    can_start = bool(process_alive and worker_ready)
    logger.info(
        "[AGENT_START_GATE_CHECK] process_alive=%s worker_ready=%s can_start=%s",
        str(process_alive).lower(),
        str(worker_ready).lower(),
        str(can_start).lower(),
    )
    if can_start:
        logger.info("[AGENT_START_GATE_ALLOWED] reason=worker_ready")
        return True, "worker_ready"
    logger.warning("[AGENT_START_GATE_BLOCKED] reason=worker_not_ready")
    return False, "worker_not_ready"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  SERVIDOR FASTAPI (roda em thread)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_LOCK_COVER_HTML = """<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;display:grid;place-items:center;
background:#05030a;color:#e9d5ff;font:200 3rem system-ui;cursor:none;user-select:none"><div style="text-align:center">
<div style="font-size:5rem;text-shadow:0 0 24px #d946ef" id="c"></div><div style="opacity:.6;font-size:1.2rem">Kira bloqueada</div></div>
<script>setInterval(function(){c.textContent=new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'})},1000)</script>"""


def build_app():
    global _satellite_registry
    from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])
    INTERFACE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=str(INTERFACE_ASSETS_DIR)), name="assets")

    from services.reminders_service import ReminderService
    from services.ui_reminders_api import create_internal_reminders_router, router as reminders_router
    from services.ui_system_stats import router as ui_system_stats_router
    from services.ui_windows import WindowRegistry, create_ui_windows_router
    from services.ui_widgets_api import create_ui_widgets_router
    from services.controllers.news import NewsResearchController
    from services.controllers.personal import AgendaController, EmailAnalysisController
    from services.providers.calendar import CalendarProvider
    from services.providers.email import EmailProvider
    from services.providers.google_oauth import GoogleAuth
    from services.secure_store import SecureStore
    from services.ui_personal_api import create_ui_personal_router
    from services.controllers.health import HealthVault
    from services.controllers.briefing import BriefingController
    from services.providers.news import NewsProvider
    from services.providers.weather import WeatherProvider
    from services.ui_health_api import create_ui_health_router
    from services.controllers.research import ResearchController
    from services.controllers.video import VideoController
    from services.controllers.weather import WeatherController
    from services.ui_presentation_api import create_ui_presentation_router
    from services.ui_actions_api import create_ui_actions_router
    from services.ui_actions_hub import UiActionsHub
    from services.media_search import get_media_search_service
    from services.ui_media_api import create_ui_media_router
    from services.image_proxy import ImageProxy, allowed_hosts_from_env
    from services.ui_img_api import create_ui_img_router
    from services.ui_web_api import create_ui_web_router
    from services.web_search import WebSearchService
    from services.election import ElectionService
    from services.downloader import DownloadManager
    from services.ui_media_settings import UiMediaSettings
    from services.panel_registry import PanelRegistry
    from services.media_story import MediaStory

    app.state.reminder_service = ReminderService(BASE_DIR / "configs" / "reminders.json")
    app.state.reminder_scheduler_task = None
    app.state.ui_actions_hub = UiActionsHub()
    app.state.ui_media_settings = UiMediaSettings(BASE_DIR / "configs" / "ui_media.json")
    app.state.media_search_service = get_media_search_service(settings=app.state.ui_media_settings)
    app.state.image_proxy = ImageProxy(BASE_DIR / "cache" / "ui_img", allowed_hosts=allowed_hosts_from_env())
    def _kira_is_speaking() -> bool:
        return str(_runtime_state_snapshot().get("agent_state") or "") == "speaking"

    async def _wait_for_narration() -> None:
        """As imagens só entram quando a Kira já está narrando (depois da primeira frase)."""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not _kira_is_speaking():
            await asyncio.sleep(0.25)
        await asyncio.sleep(2.5)

    def _visible_extra_windows() -> list[str]:
        registry = getattr(app.state, "window_registry", None)
        return registry.visible_ids() if registry is not None else []

    # Conteúdo novo abre na principal; numa janela extra à mostra só quando o Chefe pede ou já passou esse tipo para lá.
    # Celular e PC na mesma conversa: o aparelho usado por último recebe a voz e os painéis (services/remote_devices.py)
    from services.remote_devices import DeviceTracker

    def _tell_pc_device(device: str) -> None:
        def run() -> None:
            try:
                _evaluate_js_json(f"window.KiraDeviceActive && window.KiraDeviceActive({json.dumps(device)}); true")
            except RuntimeError:  # tela do PC ainda não pronta
                pass

        threading.Thread(target=run, name="kira-device-active", daemon=True).start()

    app.state.devices = DeviceTracker(on_change=_tell_pc_device)

    def _phone_media_route(command: str, value: object) -> bool:
        # "toca no computador" vai para o PC mesmo com o Chefe no celular; "no celular" vai para o celular mesmo do PC;
        # sem dizer lugar, vai para o aparelho em uso
        target = app.state.devices.wanted_device() or app.state.devices.active
        if target != "celular" or not app.state.ui_actions_hub.phone_connected or _backend_loop is None:
            return False
        action = {"type": "ui_control", "windowId": "celular", "command": command, "value": value}
        delivered = asyncio.run_coroutine_threadsafe(app.state.ui_actions_hub.publish([action]), _backend_loop).result(timeout=5)
        logger.info("PHONE_MEDIA_COMMAND command=%s delivered=%s", command, delivered)
        return bool(delivered)

    def _kira_on_tv() -> bool:
        """A Kira "está na TV": a página da TV dá sinal e ele pediu a TV como principal ou "mostra na TV" (fixar)."""
        screen = _tv_screen.SCREEN
        registry = getattr(app.state, "panel_registry", None)
        return screen.alive() and (screen.principal or bool(registry and registry.tv_sticky()))

    app.state.kira_on_tv = _kira_on_tv

    def _tv_media(command: str, value: object) -> bool:
        """Kira na TV (2026-11-07): "toca X" toca NA TV (player do YouTube embutido na página da TV, som e vídeo lá) e
        o PC fica quieto; pausar/continuar/fechar mexem nesse player. Fechar a música limpa a TV (nada de capa parada)
        e nunca tira a tela da Kira da TV."""
        screen = _tv_screen.SCREEN
        playing = screen.music if (screen.music or {}).get("tv") else None
        if command in {"stop", "close"}:
            if (screen.music or {}).get("title"):
                screen.broadcast({"t": "music"})
            return bool(playing)
        if not _kira_on_tv():
            return False
        if command == "play_item" and isinstance(value, dict) and value.get("videoId"):
            vid = str(value.get("videoId"))
            screen.broadcast({"t": "music", "tv": True, "videoId": vid, "title": str(value.get("title") or "")[:160],
                              "artist": str(value.get("channel") or "")[:120], "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "playing": True})
            from services.channel_logs import tv_log
            tv_log("TV_MUSICA_TOCANDO_NA_TV", video=vid)

            async def kick() -> None:
                # o navegador da TV bloqueia som sem gesto (testado 2026-11-07: o player parava na capa); um OK do
                # controle pela rede conta como gesto, e o play em seguida toca com som
                await asyncio.sleep(2.5)
                device = app.state.smart_home.find("tv")
                if device is not None:
                    await app.state.smart_home.command(device, "ok")
                await asyncio.sleep(0.8)
                screen.broadcast({"t": "music_cmd", "cmd": "play"})

            if _backend_loop is not None:
                asyncio.run_coroutine_threadsafe(kick(), _backend_loop)
            return True
        if playing and command in {"pause", "play", "play_pause"}:
            on = command == "play" or (command == "play_pause" and playing.get("playing") is False)
            screen.broadcast({"t": "music_cmd", "cmd": "play" if on else "pause"})
            screen.broadcast({**playing, "playing": on})
            return True
        return command == "open_tab" and value == "music" and bool(playing)

    global _PHONE_MEDIA_ROUTE
    _PHONE_MEDIA_ROUTE = lambda command, value: _tv_media(command, value) or _phone_media_route(command, value)  # noqa: E731
    transcription_bus.tagger = app.state.devices.tag
    import services.tv_screen as _tv_screen

    def _tv_music(state: dict) -> None:
        """Música da Kira na TV (se a página da TV estiver viva)."""
        if not _tv_screen.SCREEN.alive() or (_tv_screen.SCREEN.music or {}).get("tv"):
            return  # música tocando NA TV: o estado do player do PC não mexe nela
        if not state.get("isPlaying"):  # parou/pausou no PC: some da TV (antes ficava a capa parada)
            if (_tv_screen.SCREEN.music or {}).get("title"):
                _tv_screen.SCREEN.broadcast({"t": "music"})
            return
        vid = str(state.get("currentVideoId") or "")
        _tv_screen.SCREEN.broadcast({"t": "music", "title": str(state.get("currentTitle") or ""), "artist": str(state.get("currentChannel") or ""),
                                     "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "", "playing": bool(state.get("isPlaying"))})

    app.state.tv_music = _tv_music

    @app.get("/api/tv/status")
    async def tv_status(request: Request) -> dict:
        from services.local_request import is_local_request
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        return {"ok": True, "alive": _tv_screen.SCREEN.alive(), "modelo": _tv_screen.SCREEN.modelo}

    @app.post("/api/tv/state")
    async def tv_state(request: Request, payload: dict = Body(default={})) -> dict:
        """Estado/voz da Kira para o orbe da TV (a tela principal manda ~8x/s falando)."""
        from services.local_request import is_local_request
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        state = str(payload.get("state") or "idle")
        if state not in {"idle", "listening", "thinking", "speaking"} or not _tv_screen.SCREEN.alive():
            return {"ok": False, "alive": _tv_screen.SCREEN.alive()}
        if not _tv_screen.SCREEN.audio_on:  # voz saindo no PC: o orbe da TV não reage (um orbe só reage, onde ela fala)
            if (_tv_screen.SCREEN.state or {}).get("state") == "idle":
                return {"ok": True, "sent": 0}
            state, payload = "idle", {}
        level = max(0.0, min(1.0, float(payload.get("level") or 0)))
        return {"ok": True, "sent": _tv_screen.SCREEN.broadcast({"t": "state", "state": state, "level": round(level, 3)})}

    async def _tv_ready(device_name: str = "tv") -> dict:
        """O "print rápido" antes de mandar algo: a página da TV dá sinal E o navegador está na frente da TV.
        Se não, abre a tela da Kira, espera até ~12 s e confere de novo."""
        home = app.state.smart_home
        device = home.find(device_name or "tv")
        if device is None:
            return {"ok": False, "fala": "Não achei a TV na lista de aparelhos."}

        async def showing() -> bool:
            front = await home.command(device, "o que esta passando")
            return _tv_screen.SCREEN.alive() and str(front.get("app_aberto") or "") == "com.webos.app.browser"

        if await showing():
            return {"ok": True, "abriu": False}
        opened = await home.command(device, "tela_kira", "orbe")
        if not opened.get("ok"):
            return {"ok": False, "fala": opened.get("message") or opened.get("fala") or "A TV não abriu a tela da Kira."}
        for _ in range(24):
            await asyncio.sleep(0.5)
            if _tv_screen.SCREEN.alive():
                break
        return {"ok": await showing(), "abriu": True, "fala": "" if _tv_screen.SCREEN.alive() else "A TV abriu o navegador, mas a página da Kira não deu sinal (firewall do Windows na porta 3150?)."}

    @app.get("/api/logs/canal")
    async def logs_canal(request: Request, nome: str = "tv", linhas: int = 200) -> dict:
        """Últimas linhas de logs/TV ou logs/Celular (aba de logs e diagnóstico)."""
        from services.local_request import is_local_request
        from services.channel_logs import tail
        if not is_local_request(request) or request.headers.get("x-kira-remote"):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        return {"ok": True, "canal": nome, "linhas": await asyncio.to_thread(tail, nome, linhas)}

    def _tv_fold(text: str) -> str:
        import unicodedata
        return " ".join("".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)).split())

    @app.post("/api/tv/mostrar")
    async def tv_mostrar(request: Request, payload: dict = Body(default={})) -> dict:
        """"Joga esse painel / essa música / isso pra TV": confere a TV, abre se precisar e manda."""
        from services.local_request import is_local_request
        if not (is_local_request(request) or request.headers.get("x-kira-internal") == KIRA_INTERNAL_TOKEN):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        registry = app.state.panel_registry
        from services.channel_logs import tv_log
        tv_log("TV_MOSTRAR_PEDIDO", acao=payload.get("acao") or "mostrar", o_que=str(payload.get("o_que") or "")[:60])
        if payload.get("acao") == "rolar":  # "rola pra baixo/desce mais/sobe" no painel em destaque da TV
            sent = _tv_screen.SCREEN.broadcast({"t": "scroll", "dir": str(payload.get("o_que") or "baixo")[:20]}) if _tv_screen.SCREEN.alive() else 0
            return {"ok": bool(sent), "confirmed": bool(sent), "fala": "" if sent else "A tela da Kira não está na TV."}
        if payload.get("acao") == "soltar":
            registry.tv_unstick()
            _tv_screen.SCREEN.godseye = False  # Olho de Deus volta a abrir no PC
            _tv_screen.SCREEN.principal = False
            if _tv_screen.SCREEN.audio_on:  # "volta pro PC": a voz volta junto
                await asyncio.to_thread(_evaluate_js_json, _TV_PC_MUTE_JS % "false")
                _tv_screen.SCREEN.audio_on = False
                _tv_screen.SCREEN.broadcast({"t": "audio", "on": False})
            await asyncio.to_thread(_pc_orb_on_tv, False)
            return {"ok": True, "confirmed": True, "fala": "Os próximos painéis voltam a aparecer aqui."}
        ready = await _tv_ready(str(payload.get("aparelho") or "tv"))
        if not ready.get("ok"):
            return {"ok": False, "confirmed": False, **ready}
        if payload.get("acao") == "fixar":
            registry.tv_stick(600.0)
            last = registry.last_panel()  # se o conteúdo veio antes do "na TV" (ex.: briefing), já leva ele
            moved = bool(last and last.window_id != "tv" and time.monotonic() - last.updated_at < 120 and await registry.move_to_tv())
            return {"ok": True, "confirmed": True, "levou": moved, "fala": "Os próximos painéis vão para a TV por 10 minutos."}
        if payload.get("acao") in {"principal", "so_tv"}:  # "mostra só na TV a partir de agora": tudo na TV até mandar voltar
            registry.tv_stick(12 * 3600.0)
            _tv_screen.SCREEN.godseye = True
            _tv_screen.SCREEN.principal = True
            moved = await registry.move_to_tv() if registry.last_panel() else False
            return {"ok": True, "confirmed": True, "levou": moved, "fala": "A partir de agora tudo aparece na TV; o PC fica em segundo plano."}
        if payload.get("acao") == "olho_de_deus":  # o MESMO Olho de Deus do PC, rodando na TV (tvKira.js)
            from services.gods_eye import build_command
            closing = _tv_fold(str(payload.get("o_que") or "abrir")) in {"fechar", "sair", "tirar"}
            layers = [c.strip() for c in str(payload.get("camadas") or "").split(",") if c.strip()]
            if not closing:
                try:  # se estava aberto no PC, fecha lá: um globo só
                    await asyncio.to_thread(_evaluate_js_json, "(function(){if(window.KiraGodsEye&&window.KiraGodsEye.isOpen)window.KiraGodsEye.close();return '{}';})();")
                except Exception:  # noqa: BLE001
                    pass
            _tv_screen.SCREEN.godseye = True
            cmd = build_command({"action": "fechar" if closing else "abrir", "layers": layers}, None)
            result = await _call_globe("command", cmd)
            ok = isinstance(result, dict) and bool(result.get("ok"))
            return {"ok": ok, "confirmed": ok, "fala": "" if ok else "A TV não respondeu ao Olho de Deus."}
        what = str(payload.get("o_que") or "").strip()
        key = _tv_fold(what).replace("_", " ").strip()
        # "último painel" (com ou sem acento/sublinhado), "esse painel", vazio = o último aberto; senão o painel pelo nome
        # ("a imagem da carta", "o clima"). Antes "último_painel" com acento virava um cartão com esse texto (2026-11-06).
        if key in {"", "ultimo painel", "ultimo", "esse painel", "este painel", "painel", "isso", "esse", "tudo"}:
            moved = await registry.move_to_tv()
            return {"ok": moved, "confirmed": moved, "fala": "" if moved else "Não tem painel aberto para mandar."}
        if key not in {"musica", "a musica", "essa musica"} and "modelo" not in payload and not key.startswith("texto"):
            ids = registry.resolve(what)
            if ids:
                moved = False
                for panel_id in ids[-3:]:
                    moved = await registry.move_to_tv(panel_id) or moved
                return {"ok": moved, "confirmed": moved, "fala": "" if moved else "Não consegui levar esse painel."}
            if registry.last_panel() is not None:
                moved = await registry.move_to_tv()
                return {"ok": moved, "confirmed": moved, "fala": "" if moved else "Não tem painel aberto para mandar."}
        if key in {"musica", "a musica", "essa musica"}:
            what = "musica"
        if what == "musica":
            try:
                state = json.loads(MUSIC_STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if not state.get("currentTitle"):
                return {"ok": False, "confirmed": False, "fala": "Nenhuma música tocando."}
            _tv_music(state)
            return {"ok": True, "confirmed": True}
        if "modelo" in payload:
            n = max(1, min(5, int(payload.get("modelo") or 1)))
            _tv_screen.SCREEN.broadcast({"t": "modelo", "n": n})
            return {"ok": True, "confirmed": True, "modelo": n}
        await app.state.panel_registry.publish({"type": "open_panel", "kind": "quadro", "id": f"quadro-tv-{secrets.token_hex(3)}",
                                                 "title": what[:60], "windowId": "tv", "props": {"tipo": "info", "titulo": what[:120], "resumo": what[:600]}})
        return {"ok": True, "confirmed": True}

    def _tv_audio_token() -> dict | None:
        """Token SÓ-OUVINTE da sala de voz atual para a página da TV tocar a voz da Kira (2026-11-06)."""
        from livekit.api import AccessToken, VideoGrants
        room_name = str(getattr(app.state, "pc_room", "") or "")
        credential = _resolve_livekit_credential_for_room(room_name) if room_name else None
        if credential is None:
            return None
        token = (AccessToken(api_key=credential.api_key, api_secret=credential.api_secret)
                 .with_identity(f"tv-{secrets.token_hex(3)}").with_name("TV").with_ttl(timedelta(minutes=2))  # só para entrar; quem entrou fica
                 .with_grants(VideoGrants(room_join=True, room=room_name, can_publish=False, can_subscribe=True,
                                          can_publish_data=False)).to_jwt())
        return {"url": credential.url, "token": token}

    # O PC fica mudo pelo mesmo caminho do modo celular (volume do áudio da Kira em 0; o orbe continua lendo a faixa).
    _TV_PC_MUTE_JS = ("(function(on){window.__kiraAudioOnTv=on;if(!window.__kiraTvWrap&&typeof applyAudioPreferences==='function'){"
                      "window.__kiraTvWrap=1;var o=applyAudioPreferences;applyAudioPreferences=async function(){await o();"
                      "if(window.__kiraAudioOnTv)agentAudioEls.forEach(function(a){a.volume=0;});};}"
                      "if(typeof applyAudioPreferences==='function')applyAudioPreferences();return JSON.stringify({ok:true});})(%s)")

    # Kira na TV como principal (2026-11-07): o orbe do PC para de desenhar (aba Aparência pausada, galáxia segura e
    # o globo 3D escondido: sem caixa à mostra o laço dele para sozinho) e volta ao normal quando ela sai da TV.
    _PC_ORB_TV_JS = ("(function(on){document.documentElement.classList.toggle('kira-on-tv',on);"
                     "try{window.KiraAppearance&&window.KiraAppearance.setPaused(on);}catch(e){}"
                     "try{window.kira3D&&window.kira3D.hold&&window.kira3D.hold('tv',on);}catch(e){}"
                     "var g=document.getElementById('kira-orb3d-canvas');if(g)g.style.display=on?'none':"
                     "(document.documentElement.dataset.kiraOrb==='globo'?'block':'none');"
                     "return JSON.stringify({ok:true});})(%s)")

    def _pc_orb_on_tv(on: bool) -> None:
        try:
            _evaluate_js_json(_PC_ORB_TV_JS % ("true" if on else "false"))
        except Exception:  # noqa: BLE001 - tela do PC ainda não pronta
            pass

    def _tv_gone() -> None:
        """A tela da Kira saiu da TV (restaurou ou a TV sumiu): voz volta ao PC, orbe volta a animar, painéis no PC."""
        screen = _tv_screen.SCREEN
        if screen.audio_on:
            try:
                _evaluate_js_json(_TV_PC_MUTE_JS % "false")
            except Exception:  # noqa: BLE001
                pass
            screen.audio_on = False
            screen.broadcast({"t": "audio", "on": False})
        screen.godseye = False
        _pc_orb_on_tv(False)
        registry = getattr(app.state, "panel_registry", None)
        if registry is not None:
            registry.tv_unstick()
        from services.channel_logs import tv_log
        tv_log("TV_SAIU_TUDO_VOLTA_PC")

    _tv_screen.SCREEN.on_gone = _tv_gone

    @app.post("/api/tv/audio")
    async def tv_audio(request: Request, payload: dict = Body(default={})) -> dict:
        """"Fala pela TV": a página da TV entra na sala como ouvinte e o PC fica mudo; "volta pro fone" desfaz."""
        from services.local_request import is_local_request
        if not (is_local_request(request) or request.headers.get("x-kira-internal") == KIRA_INTERNAL_TOKEN):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        on = bool(payload.get("ligar", True))
        if on:
            if not getattr(app.state, "pc_room", ""):
                return {"ok": False, "confirmed": False, "fala": "A sessão de voz não está ativa; inicie a sessão primeiro."}
            ready = await _tv_ready(str(payload.get("aparelho") or "tv"))
            if not ready.get("ok"):
                return {"ok": False, "confirmed": False, **ready}
            screen = _tv_screen.SCREEN
            screen.audio_token, screen.audio_status = _tv_audio_token, ("", 0.0)
            # o token só sai para o IP da TV: outro aparelho da rede não pega um ouvinte da sala (que ouve o microfone também)
            _tv_dev = app.state.smart_home.find(str(payload.get("aparelho") or "tv"))
            screen.audio_ip = getattr(_tv_dev, "ip", "") or ""
            started = time.monotonic()
            screen.audio_on = True
            screen.broadcast({"t": "audio", "on": True})  # a TV entra na hora (o polling de 3 s fica de reserva)
            # O PC só fica mudo DEPOIS que a TV confirmar que toca a faixa da Kira: mutar antes engolia a 1ª fala
            # (2026-11-06). A resposta da ferramenta também espera, para a confirmação já sair na TV.
            while time.monotonic() - started < 6.0:
                status, at = screen.audio_status
                if at >= started and status in {"playing", "blocked"}:
                    break
                await asyncio.sleep(0.15)
            status, at = screen.audio_status
            if at < started or status != "playing":
                screen.audio_on = False
                screen.broadcast({"t": "audio", "on": False})
                fala = ("A TV bloqueou o som: aperte OK no controle da TV e peça de novo." if status == "blocked" and at >= started
                        else "A TV não confirmou o som a tempo; continuo falando pelo PC.")
                return {"ok": False, "confirmed": False, "fala": fala}
            await asyncio.to_thread(_evaluate_js_json, _TV_PC_MUTE_JS % "true")
            await asyncio.to_thread(_pc_orb_on_tv, True)  # voz na TV: o orbe do PC encolhe e para
            from services.channel_logs import tv_log
            tv_log("TV_AUDIO_LIGADO", ms_ate_tocar=int((at - started) * 1000))
            return {"ok": True, "confirmed": True, "audio": "tv", "ms_ate_tocar": int((at - started) * 1000)}
        # volta pro fone: desmuta o PC ANTES de a TV sair, para não haver buraco sem som
        await asyncio.to_thread(_evaluate_js_json, _TV_PC_MUTE_JS % "false")
        await asyncio.to_thread(_pc_orb_on_tv, False)  # voz de volta no PC: o orbe do PC volta a reagir
        _tv_screen.SCREEN.audio_on = False
        _tv_screen.SCREEN.broadcast({"t": "audio", "on": False})
        from services.channel_logs import tv_log
        tv_log("TV_AUDIO_DESLIGADO")
        return {"ok": True, "confirmed": True, "audio": "pc"}

    async def _floating_when_hidden() -> str | None:
        """Kira minimizada ou na bandeja e nenhuma janela extra à mostra: painel vai para a janela flutuante (sempre por
        cima de qualquer programa), criada na hora no monitor mais livre (2026-10-31)."""
        if not await asyncio.to_thread(_main_window_hidden):
            return None
        registry = getattr(app.state, "window_registry", None)
        if registry is None:
            return None
        if any(registry._windows[w].name != "Kira flutuante" for w in registry.visible_ids()):
            return None  # janela extra normal à mostra: o painel vai para ela
        record = await registry.create_floating()
        return str(record.get("windowId") or record.get("id") or "") or None

    _hub_publish = app.state.ui_actions_hub.publish

    async def _publish_tv_aware(actions: list) -> int:
        """Aba Casa pedida com a Kira na TV abre na TV (WhatsApp e configurações nunca vão para lá)."""
        if not _kira_on_tv():
            return await _hub_publish(actions)
        rest = []
        for action in actions:
            if action.get("type") == "ui_control" and action.get("command") == "open_tab" and action.get("value") == "casa":
                _tv_screen.SCREEN.broadcast({"t": "ui", "action": action})
            else:
                rest.append(action)
        return (await _hub_publish(rest) if rest else 0) + (len(actions) - len(rest))

    app.state.ui_actions_hub.publish = _publish_tv_aware

    app.state.panel_registry = PanelRegistry(
        app.state.ui_actions_hub.publish, is_busy=_kira_is_speaking, visible_windows=_visible_extra_windows,
        phone_active=lambda: app.state.devices.active == "celular" and app.state.ui_actions_hub.phone_connected,
        floating_window=_floating_when_hidden,
        tv_alive=_tv_screen.SCREEN.alive, tv_send=_tv_screen.SCREEN.broadcast,  # TV como tela extra (2026-11-06)
    )
    app.state.media_story = MediaStory(app.state.media_search_service, app.state.panel_registry, prefetch=app.state.image_proxy.fetch, gate=_wait_for_narration)
    app.state.window_registry = WindowRegistry(
        opener=_open_satellite_window,
        publish=app.state.ui_actions_hub.publish,
        port=PORT,
        profiles_path=BASE_DIR / "configs" / "windows.json",
    )
    _satellite_registry = app.state.window_registry
    app.state.window_registry.ready = app.state.ui_actions_hub.has_window
    app.include_router(create_ui_windows_router(app.state.window_registry, token_provider=lambda: KIRA_INTERNAL_TOKEN, panels=app.state.panel_registry,
                                                main_window=lambda: _webview_win))
    from services.ui_widgets_api import WidgetData

    app.state.widget_data = WidgetData()  # widgets da tela e as seções novas do briefing (cotações, hoje na história)
    app.include_router(create_ui_widgets_router(app.state.widget_data))
    from services.google_account import create_google_router

    app.include_router(create_google_router(PORT))  # conta Google principal: o YouTube do Chefe na aba Música · Vídeos
    # Pelo registro: notícias, clima e briefing entram no prazo de tela, na troca por assunto e em list_active_panels.
    app.state.research = ResearchController(app.state.panel_registry.publish)
    weather_provider, news_provider = WeatherProvider(), NewsProvider()
    app.state.weather = WeatherController(app.state.research, weather_provider)
    app.state.news = NewsResearchController(app.state.research, news_provider)
    app.state.video = VideoController(app.state.research)
    app.include_router(create_ui_presentation_router(app.state.research, app.state.weather, app.state.news, app.state.video, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    # Céu e radar perto do Chefe (ISS, satélites, chuva nos próximos minutos, avião passando, foto de satélite)
    from services.controllers.weather import LocationStore
    from services.sky_api import create_sky_router
    from services.sky_watch import SkyWatch

    app.state.sky = SkyWatch(BASE_DIR / "cache" / "sky", location_city=LocationStore().get)
    app.include_router(create_sky_router(app.state.sky, publish=app.state.panel_registry.publish, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    # Manchetes de um veículo, ler matéria, cotações e o que o mundo noticia (GDELT)
    from services.info_api import create_info_router
    from services.info_feeds import InfoFeeds

    app.state.info_feeds = InfoFeeds(BASE_DIR / "configs" / "rss_feeds.json")
    app.include_router(create_info_router(app.state.info_feeds, publish=app.state.panel_registry.publish, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    secure_store = SecureStore()
    # Avisos para fora (ntfy, Telegram, Discord, e-mail...): endereços com chave ficam no cofre
    from services.notify import Notifier
    from services.notify_api import create_notify_router

    from services.web_push import WebPush

    app.state.web_push = WebPush(secure_store)  # notificação no app da Kira instalado no celular
    app.state.notifier = Notifier(secure_store, push=app.state.web_push)

    def _notice_on_tv(title: str, body: str) -> None:
        # aviso que sai com a Kira na TV aparece na TV também (nada do WhatsApp: conversa dele não vai para a TV)
        if app.state.kira_on_tv() and "whatsapp" not in f"{title} {body}".lower():
            _tv_screen.SCREEN.broadcast({"t": "aviso", "title": str(title)[:60], "text": str(body)[:300]})

    app.state.notifier.mirror = _notice_on_tv
    app.include_router(create_notify_router(app.state.notifier, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    google_auths = {
        service: GoogleAuth(service, secure_store, redirect_uri=f"http://127.0.0.1:{PORT}/api/ui/google/callback/{service}")
        for service in ("calendar", "email")
    }
    calendar_provider, email_provider = CalendarProvider(google_auths["calendar"]), EmailProvider(google_auths["email"])
    app.state.agenda = AgendaController(app.state.research, calendar_provider)
    app.state.email = EmailAnalysisController(app.state.research, email_provider)
    app.state.briefing = BriefingController(
        app.state.research, weather=weather_provider, locations=app.state.weather.locations, news=news_provider,
        calendar=calendar_provider, email=email_provider, reminders=app.state.reminder_service.list, widgets=app.state.widget_data,
    )
    app.include_router(create_ui_personal_router(app.state.agenda, app.state.email, google_auths, app.state.briefing, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    app.include_router(create_ui_health_router(app.state.research, HealthVault(secure_store), token_provider=lambda: KIRA_INTERNAL_TOKEN))
    from services.kitchen import Kitchen
    from services.kitchen_api import create_kitchen_router

    # Assistente de cozinha: receita de verdade, passo a passo e timers (alarme dos lembretes)
    async def _kitchen_generate(prompt: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            _text_only_chat_executor,
            lambda: _gemini_text_sync(prompt, reason="kitchen", tag="KITCHEN", max_rotations=4, json_mode=True, think=False,
                                      models=(TEXT_ONLY_GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"),
                                      http_timeout_ms=30000),
        )

    app.state.kitchen = Kitchen(
        generate=_kitchen_generate,
        create_alarm=lambda text, when, phone: app.state.reminder_service.create(
            text, when.isoformat(timespec="seconds"), kind="alarm", notify_phone=phone).id,
        delete_alarm=app.state.reminder_service.delete,
        phone_active=lambda: app.state.devices.active == "celular",
    )
    app.include_router(create_kitchen_router(app.state.kitchen, app.state.research, token_provider=lambda: KIRA_INTERNAL_TOKEN,
                                             publish=app.state.panel_registry.publish))
    app.include_router(create_ui_img_router(app.state.image_proxy))
    app.include_router(
        create_ui_web_router(
            app.state.panel_registry,
            token_provider=lambda: KIRA_INTERNAL_TOKEN,
            web=WebSearchService(media=app.state.media_search_service),
            election=ElectionService(),
            downloads=DownloadManager(),
        )
    )
    app.include_router(reminders_router)
    app.include_router(create_internal_reminders_router(lambda: KIRA_INTERNAL_TOKEN, phone_active=lambda: app.state.devices.active == "celular"))
    app.include_router(ui_system_stats_router)
    app.include_router(
        create_ui_actions_router(
            app.state.ui_actions_hub,
            token_provider=lambda: KIRA_INTERNAL_TOKEN,
            port=PORT,
        )
    )
    app.include_router(
        create_ui_media_router(
            app.state.panel_registry,
            token_provider=lambda: KIRA_INTERNAL_TOKEN,
            service=app.state.media_search_service,
            story=app.state.media_story,
            settings=app.state.ui_media_settings,
            windows_summary=app.state.window_registry.describe,
        )
    )
    from services.image_creation import ImageCreator, create_image_creation_router

    app.state.image_creator = ImageCreator(app.state.panel_registry.publish)
    app.include_router(create_image_creation_router(app.state.image_creator, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    from services.whatsapp_secretary import WhatsAppSecretary

    # Secretária do WhatsApp: roda aqui no backend (fora do processo de voz) e só fala com a ponte local.
    async def _secretary_fetch() -> list:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://127.0.0.1:5050/messages/new/secretary", headers=_whatsapp_bridge_headers())
            return (resp.json().get("messages") or []) if resp.status_code == 200 else []
        except Exception:
            return []  # ponte reiniciando: tenta na próxima volta

    async def _secretary_reply(jid: str, text: str) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post("http://127.0.0.1:5050/reply", json={"jid": jid, "message": text}, headers=_whatsapp_bridge_headers())
            return resp.status_code == 200 and bool(resp.json().get("success"))
        except Exception:
            return False

    async def _secretary_generate(prompt: str) -> str:
        # 2026-11-06: thread própria (antes esperava na fila do chat por texto) e modelo leve primeiro com 10 s por
        # tentativa: o 2.5-flash e o flash-latest caíam com ServerError e levavam 11 s antes do lite responder.
        return await asyncio.to_thread(
            lambda: _gemini_text_sync(
                prompt,
                reason="whatsapp_secretary",
                tag="SECRETARY_GEMINI",
                max_rotations=3,
                models=("gemini-2.5-flash-lite", TEXT_ONLY_GEMINI_MODEL, "gemini-flash-lite-latest"),
                http_timeout_ms=10000,  # mínimo aceito pelo Gemini (8 s dava 400 INVALID_ARGUMENT)
            ),
        )

    def _secretary_style() -> str:
        # personalidade do Chefe (Configurações > Personalidade), sem o apelido do dono: quem lê é o contato
        from services.kira_settings import personality_prompt
        return re.sub(r"Chame o dono de [^.]*\.\)?\.?", "", personality_prompt()).strip()

    def _secretary_audio_on() -> bool:
        try:  # "secretary_audio": false em configs/whatsapp_runtime.json desliga os áudios
            return json.loads(WHATSAPP_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")).get("secretary_audio", True) is not False
        except (OSError, ValueError):
            return True

    async def _secretary_voice(jid: str, text: str) -> bool:
        """Fala com a voz da Kira e manda como mensagem de voz; False = mande escrito."""
        if not _secretary_audio_on():
            return False
        from services.whatsapp_voice import synthesize
        key, _ = activate_current_gemini_api_key(logger_override=logger, reason="whatsapp_voz", key_type="text", update_env=False)
        if not key:
            return False
        # Limite só na geração (12 s). Estourou: o arquivo que ainda sair da thread é apagado quando ficar pronto.
        job = asyncio.ensure_future(asyncio.to_thread(synthesize, text, key))
        try:
            audio = await asyncio.wait_for(asyncio.shield(job), timeout=12.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            job.add_done_callback(lambda f: f.cancelled() or f.exception() or f.result().unlink(missing_ok=True))
            raise

        async def send() -> bool:
            # depois de gerado, o envio não é cancelado no meio (senão a ponte entregava o áudio e ia o texto também)
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post("http://127.0.0.1:5050/reply", json={"jid": jid, "message": text, "audio": str(audio)}, headers=_whatsapp_bridge_headers())
                return resp.status_code == 200 and bool(resp.json().get("success"))
            finally:
                audio.unlink(missing_ok=True)

        return await asyncio.shield(asyncio.ensure_future(send()))

    async def _secretary_presence(jid: str, kind: str) -> None:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post("http://127.0.0.1:5050/presence", json={"jid": jid, "kind": kind}, headers=_whatsapp_bridge_headers())

    def _secretary_see_sync(path: str) -> str:
        """Foto/figurinha/vídeo recebido no WhatsApp: descrição curta pelo Gemini (visão)."""
        from google import genai
        from google.genai import types as genai_types
        file = Path(path)
        if not file.is_file() or file.stat().st_size > 16 * 1024 * 1024:
            return ""
        mime = {".webp": "image/webp", ".mp4": "video/mp4", ".png": "image/png"}.get(file.suffix.lower(), "image/jpeg")
        key, _ = activate_current_gemini_api_key(logger_override=logger, reason="whatsapp_media", key_type="text", update_env=False)
        client = genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=20000))
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=[genai_types.Part.from_bytes(data=file.read_bytes(), mime_type=mime),
                      "Descreva em português, em até 2 frases curtas, o que aparece (se for figurinha, a emoção/piada dela)."],
        )
        return _extract_gemini_response_text(response) or ""

    async def _secretary_see(path: str) -> str:
        return await asyncio.to_thread(_secretary_see_sync, path)

    def _emergency_config() -> dict:
        """configs/emergencia.json (Configurações > Emergência): folgas, turno, horário de sono, contatos de emergência."""
        try:
            return json.loads((BASE_DIR / "configs" / "emergencia.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _secretary_situation() -> str:
        """Situação GERAL do Chefe para a Kira dizer no WhatsApp (de folga, trabalhando, dormindo)."""
        cfg = _emergency_config()
        now = datetime.now()
        dias = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
        folgas = {str(d).lower().replace("á", "a").replace("ç", "c") for d in cfg.get("folgas") or ["sabado", "domingo"]}
        folga = dias[now.weekday()] in folgas or now.strftime("%Y-%m-%d") in folgas
        hm = now.strftime("%H:%M")
        def dentro(faixa: str) -> bool:
            try:
                ini, fim = [x.strip() for x in str(faixa).split("-")]
            except ValueError:
                return False
            return ini <= hm < fim if ini <= fim else (hm >= ini or hm < fim)
        partes = [f"agora são {hm}", "hoje é folga dele" if folga else "hoje ele trabalha"]
        if dentro(cfg.get("dorme") or "23:00-07:30"):
            partes.append("ele deve estar dormindo")
        elif not folga and dentro(cfg.get("turno") or "07:00-17:00"):
            partes.append("ele deve estar no trabalho")
        return "; ".join(partes) + "."

    async def _secretary_emergency(jid: str, name: str, said: str) -> None:
        """Contato de emergência relatou algo grave: se o Chefe está de folga e em casa, liga a TV, abre a Kira nela,
        chama por voz algumas vezes e avisa o contato. Só para quem está em contatos_emergencia."""
        cfg = _emergency_config()
        contatos = [str(c).lower() for c in cfg.get("contatos_emergencia") or []]
        if not any(c and c in name.lower() for c in contatos):
            logger.info("WHATSAPP_EMERGENCY_IGNORED contact=%s reason=nao_e_contato_de_emergencia", name)
            return
        situation = _secretary_situation()
        logger.warning("WHATSAPP_EMERGENCY contact=%s situation=%s", name, situation)
        if "folga" not in situation or not cfg.get("em_casa_na_folga", True):
            await _secretary_reply(jid, f"Kira: Avisei aqui, mas o Chefe não está em casa agora ({situation.split(';')[1].strip()}). Ele vê assim que puder.")
            return
        home = getattr(app.state, "smart_home", None)
        tv = home.find("tv") if home else None
        if tv is not None:
            for action in ("ligar", "tela_kira"):
                try:
                    await home.command(tv, action, "orbe")
                except Exception:  # noqa: BLE001 - TV fora: segue chamando pelo PC
                    pass
                await asyncio.sleep(6)
        await _secretary_reply(jid, "Kira: Liguei a TV e estou chamando o Chefe pela casa agora. Me conta o que aconteceu e onde você está.")
        reminders = getattr(app.state, "reminder_service", None)
        for tentativa in range(3):  # chama, espera, chama de novo
            if reminders is not None:
                try:
                    reminders.create(f"CHEFE, ACORDA! Emergência: {name} disse no WhatsApp: {said[:160]}. Responde pra ela!",
                                     datetime.now().isoformat(timespec="seconds"), "reminder", "none")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WHATSAPP_EMERGENCY_CALL_FAILED reason=%s", type(exc).__name__)
            await asyncio.sleep(90)

    async def _secretary_notify(name: str, said: str) -> None:
        """Contato insistindo no WhatsApp: recado salvo, notificação no celular e aviso falado quando o Chefe estiver."""
        logger.info("WHATSAPP_RECADO contact=%s", name)
        try:
            with (BASE_DIR / "data" / "recados_whatsapp.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"quando": datetime.now().isoformat(timespec="seconds"), "contato": name, "texto": said}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            await app.state.web_push.send(f"{name} está te chamando", said[:180], tag="recado")
        except Exception:  # noqa: BLE001 - sem celular inscrito
            pass
        try:
            app.state.reminder_service.create(f"Recado do WhatsApp: {name} insistiu em falar com você. Disse: {said[:200]}",
                                              datetime.now().isoformat(timespec="seconds"), "reminder", "none")
        except Exception:  # noqa: BLE001
            pass

    app.state.whatsapp_secretary = WhatsAppSecretary(
        notify=_secretary_notify,
        see=_secretary_see, situation=_secretary_situation, emergency=_secretary_emergency,
        fetch=_secretary_fetch,
        reply=_secretary_reply,
        generate=_secretary_generate,
        style=_secretary_style,
        voice=_secretary_voice,
        presence=_secretary_presence,
        active=lambda: is_whatsapp_enabled() and get_whatsapp_mode() == "secretary" and _is_whatsapp_process_running(),
    )
    app.state.whatsapp_secretary_stop = asyncio.Event()
    app.state.whatsapp_secretary_task = None
    from services.browser_task import NOTES_FILE as BROWSER_NOTES_FILE, BrowserTasks
    from services.live_browser import LiveBrowser
    from services.live_browser_api import control as live_browser_control, create_live_browser_router

    # Navegador da Kira ao vivo (aba Internet): Chromium sem janela, perfil fora do projeto (logins e cookies).
    _browser_features = (load_kira_features().get("features") or {}).get("browser") or {}
    app.state.live_browser = LiveBrowser(
        Path(os.getenv("KIRA_BROWSER_PROFILE_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Kira" / "browser_profile"),
        idle_seconds=float(_browser_features.get("idle_shutdown_seconds") or 300),
    )

    async def _browser_task_generate(prompt: str) -> str:
        # mesma thread única do chat por texto e da secretária (a troca de chave não tem trava entre threads)
        return await asyncio.get_running_loop().run_in_executor(
            _text_only_chat_executor,
            lambda: _gemini_text_sync(
                prompt,
                reason="browser_task",
                tag="BROWSER_TASK_GEMINI",
                max_rotations=4,
                # 2.5-flash primeiro: o lite errava tarefas de várias páginas (buscar, comparar, baixar) e o
                # flash-latest dava erro de servidor/tempo a cada passo; o lite fica de reserva quando a cota acaba.
                models=(TEXT_ONLY_GEMINI_MODEL, "gemini-flash-lite-latest"),
                http_timeout_ms=20000,
                json_mode=True,
            ),
        )

    app.state.browser_tasks = BrowserTasks(browser=app.state.live_browser, generate=_browser_task_generate, notes_file=BROWSER_NOTES_FILE)
    app.include_router(create_live_browser_router(app.state.live_browser, app.state.browser_tasks, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    from services.instagram import Instagram
    from services.instagram_api import LOGIN_URL as _INSTAGRAM_LOGIN_URL, create_instagram_router

    # Instagram pela sessão do navegador da Kira (login feito pelo Chefe na aba Internet; o Instagram recusa login automático)
    app.state.instagram = Instagram(
        get=lambda url, headers: app.state.live_browser.session_get(url, headers),
        user_id=lambda: app.state.live_browser.session_cookie("www.instagram.com", "ds_user_id"),
        state_path=BASE_DIR / "memory" / "local" / "instagram.json",
    )

    async def _instagram_open_login() -> None:
        await app.state.live_browser.navigate(_INSTAGRAM_LOGIN_URL)
        await asyncio.to_thread(_dispatch_kira_ui_action, {"action": "open_tab", "target": "browser"})

    app.include_router(create_instagram_router(app.state.instagram, app.state.research, token_provider=lambda: KIRA_INTERNAL_TOKEN,
                                               open_login=_instagram_open_login))

    from services.gods_eye import GodsEyeData as _RouteGeocoder
    from services.routes import RoutePlanner
    from services.routes_api import create_routes_router

    # Rotas e tempo de viagem: a rota flutua na tela num mapa com zoom (routeView.js), no modelo escolhido pelo Chefe
    _route_geocoder = _RouteGeocoder()
    app.state.routes = RoutePlanner(_route_geocoder.geocode, geocode_near=_route_geocoder.geocode_near)
    async def _show_route(arg: dict) -> object:
        # Chefe no celular: a rota flutua lá (ida e volta pela conexão dos painéis); senão, na tela do PC
        if app.state.devices.active == "celular" and app.state.ui_actions_hub.phone_connected:
            return await app.state.phone_rpc.call("rota", "show", arg)
        js = (f"(function(){{ if (!window.KiraRouteOverlay) return JSON.stringify({{ok:false,error:'rota_indisponivel'}}); "
              f"return JSON.stringify(window.KiraRouteOverlay.show({json.dumps(arg)})); }})();")
        return await asyncio.to_thread(_evaluate_js_json, js)

    app.include_router(create_routes_router(app.state.routes, token_provider=lambda: KIRA_INTERNAL_TOKEN, show=_show_route))

    from services.documents_api import create_documents_router
    from services.pc_operator import PcOperator
    from services.pc_operator_api import create_pc_operator_router

    # Programas do Windows: por dentro (sem mouse/teclado) e mouse/teclado de verdade só com o Chefe liberando
    app.state.pc_operator = PcOperator()
    app.include_router(create_pc_operator_router(app.state.pc_operator, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    from services.pc_health import HealthWatch, create_pc_health_router

    # Saúde do PC: vigia em segundo plano (limpa a RAM sozinha acima do limite com o Reduce Memory do Chefe)
    app.state.pc_health = HealthWatch()
    app.include_router(create_pc_health_router(app.state.pc_health, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    from services.smart_home import SmartHome
    from services.smart_home_api import create_smart_home_router

    # Casa inteligente pelo Wi-Fi: descobre os aparelhos da rede e controla TV LG / Chromecast
    app.state.smart_home = SmartHome()
    app.include_router(create_smart_home_router(app.state.smart_home, token_provider=lambda: KIRA_INTERNAL_TOKEN))

    @app.on_event("startup")
    async def _start_pc_health() -> None:
        app.state.pc_health.start()

    @app.on_event("startup")
    async def _start_log_retention() -> None:
        """24/7: a cada 6 h, log com mais de 2 dias vira resumo em memory/local/resumos e o bruto sai."""
        async def _loop() -> None:
            from services.kira_log_manager import cleanup_old_logs
            while True:
                await asyncio.sleep(300)
                try:
                    await asyncio.to_thread(cleanup_old_logs, days=2)
                except Exception as exc:  # noqa: BLE001 - nunca derruba o app
                    logger.warning("LOG_RETENTION_FAILED reason=%s", type(exc).__name__)
                await asyncio.sleep(6 * 3600 - 300)
        app.state.log_retention_task = asyncio.create_task(_loop())

    @app.on_event("startup")
    async def _start_idle_cloud_to_local() -> None:
        """24/7 (2026-11-07): parada há cloud_to_local_after_minutes na nuvem, a sessão passa para o LiveKit LOCAL
        (sem minutos de cota). A regra existia, mas nada a chamava. Volta pela fala/texto normal ou pelas palavras
        de despertar do modo econômico; a nuvem só é usada de novo quando pedida (ou pelo celular)."""
        async def _loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    manager = get_economy_mode_manager()
                    cfg = manager.get_config()
                    minutes = int((cfg or {}).get("cloud_to_local_after_minutes") or 60)
                    idle = max(time.time() - float(getattr(manager, "_last_interaction_at", time.time())), 0.0)
                    if not (cfg or {}).get("enable_real_cloud_to_local_handoff"):
                        continue  # trava desligada: não tenta (antes registrava BLOCKED a cada minuto)
                    if idle < minutes * 60 or provider_kind_from_snapshot(_runtime_state_snapshot()) != "cloud":
                        continue
                    if any(_economy_transport_blockers_snapshot().values()) or _get_active_transport_switch():
                        continue
                    logger.info("IDLE_CLOUD_TO_LOCAL_BEGIN idle_min=%d", int(idle // 60))
                    result = await _execute_economy_transport_switch("local", source="idle_auto")
                    logger.info("IDLE_CLOUD_TO_LOCAL_RESULT ok=%s", bool((result or {}).get("ok")))
                except Exception as exc:  # noqa: BLE001 - nunca derruba o app
                    logger.warning("IDLE_CLOUD_TO_LOCAL_FAILED reason=%s", type(exc).__name__)
        app.state.idle_cloud_to_local_task = asyncio.create_task(_loop())

    # Documentos e PDF por voz (converter, juntar, separar, criar, ler); sem lugar claro salva em Documentos\Kira
    async def _work_generate(prompt: str) -> str:
        # mesmo caminho das tarefas do navegador, com mais tempo: um trabalho inteiro é uma resposta grande
        return await asyncio.get_running_loop().run_in_executor(_text_only_chat_executor, lambda: _gemini_text_sync(
            prompt, reason="school_work", tag="SCHOOL_WORK_GEMINI", max_rotations=4,
            models=(TEXT_ONLY_GEMINI_MODEL, "gemini-flash-lite-latest"), http_timeout_ms=90000, json_mode=True))

    _work_notified: dict[str, float] = {}

    def _work_notify(text: str) -> None:
        now = time.monotonic()
        for key, at in list(_work_notified.items()):
            if now - at > 600:
                _work_notified.pop(key, None)
        if text in _work_notified:  # mesmo trabalho concluído avisado de novo (retentativa/callback duplo)
            logger.info("SCHOOL_WORK_NOTIFY_DUPLICATE_SKIPPED")
            return
        _work_notified[text] = now
        try:  # vira lembrete de agora: a Kira fala e aparece no sininho
            app.state.reminder_service.create(text, datetime.now().astimezone().isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001
            logger.info("SCHOOL_WORK_NOTIFY_FAILED reason=%s", exc)

    app.include_router(create_documents_router(token_provider=lambda: KIRA_INTERNAL_TOKEN, generate=_work_generate, notify=_work_notify))
    from services.tutor import Tutor, create_tutor_router  # professora: biblioteca, simulado, correção (Fase 3)
    from services.education import EducationStore, create_education_router

    # O workspace usa o mesmo SQLite do Tutor: biblioteca/FTS/simulados e o
    # histórico pedagógico são uma única fonte de verdade para voz e navegador.
    app.state.tutor = Tutor()
    from services.kira_skills import create_skills_router  # skills (SKILL.md): arrastar, GitHub, consultar (2026-11-05)
    app.include_router(create_skills_router())
    from services.quadro_panels import create_quadro_router  # painéis criados na hora pela Kira (2026-11-05)
    app.include_router(create_quadro_router(publish=app.state.panel_registry.publish))
    from services.design_tools import create_design_router  # PDF diagramado, gráfico, diagrama, QR (2026-11-05)
    app.include_router(create_design_router(publish=app.state.panel_registry.publish, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    app.state.education = EducationStore(user_id=os.getenv("KIRA_USER_ID", "alan"))
    app.include_router(create_tutor_router(app.state.tutor, generate=_work_generate, publish=app.state.panel_registry.publish, token_provider=lambda: KIRA_INTERNAL_TOKEN, education=lambda: app.state.education))
    from services.knowledge_drop import create_knowledge_router  # arquivo solto na tela vira conhecimento (2026-11-05)
    app.include_router(create_knowledge_router(tutor=app.state.tutor, generate=_work_generate, notify=_work_notify))

    async def _education_generate_quiz(course: str, topic: str, count: int, banca: str) -> dict[str, Any]:
        return await app.state.tutor.make_quiz(_work_generate, course, topic, count, banca)

    async def _education_grade_quiz(quiz_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        quiz = app.state.tutor.quiz(quiz_id)
        result = await app.state.tutor.grade(_work_generate, quiz_id, answers)
        result["curso"] = quiz.get("curso", "")
        return result

    app.include_router(create_education_router(
        app.state.education,
        token_provider=lambda: KIRA_INTERNAL_TOKEN,
        research=app.state.tutor.research,
        ingest=app.state.tutor.ingest,
        quiz_generate=_education_generate_quiz,
        quiz_grade=_education_grade_quiz,
        forget_course=app.state.tutor.forget_course,
        quiz_export=app.state.tutor.export,
        publish=app.state.panel_registry.publish,
    ))

    # Memória de longo prazo: preferências contextualizadas, procedimentos e
    # experiências confirmadas ficam no SQLite local e não alteram o código-fonte.
    from services.experience_memory import ExperienceMemory, create_memory_router

    app.state.experience_memory = ExperienceMemory(user_id=os.getenv("KIRA_USER_ID", "alan"))
    app.include_router(create_memory_router(
        app.state.experience_memory,
        token_provider=lambda: KIRA_INTERNAL_TOKEN,
        publish=app.state.panel_registry.publish,
    ))

    from services.remote_access import RemoteGateway, RemoteSettings
    from services.remote_access_api import create_remote_access_router

    # Acesso pelo celular: portão com senha na porta 3100 (a senha, só o hash, fica em configs/secure, fora do git).
    app.state.remote_settings = RemoteSettings(CONFIGS_DIR / "secure" / "remote_access.json")
    app.state.remote_gateway = RemoteGateway(app.state.remote_settings)
    # Perfis comuns do celular (até 3 com o do Chefe): conversa por texto com memória separada (services/guest_chat.py)
    from services.controllers.weather import LocationStore as _GuestCity
    from services.guest_api import create_guest_router
    from services.guest_chat import GuestChat

    async def _guest_generate(prompt: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            _text_only_chat_executor,
            lambda: _gemini_text_sync(prompt, reason="guest_chat", tag="GUEST_CHAT", max_rotations=4, json_mode=True, think=False,
                                      models=(TEXT_ONLY_GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"),
                                      http_timeout_ms=20000),
        )

    app.state.guest_chat = GuestChat(generate=_guest_generate, owner_name=os.getenv("KIRA_OWNER_NAME", "Chefe") or "Chefe",
                                     city=_GuestCity().get)
    from services.guest_tools import GuestReminders, GuestTools
    from services.kira_voice import KiraVoice
    from services.kitchen import generate_recipe

    def _gemini_tts_sync(text: str, voice: str) -> bytes:
        """Voz da Kira pelo Gemini TTS (chave de texto); em cota, gira a chave uma vez."""
        from google import genai
        from google.genai import types as genai_types

        for _attempt in range(2):
            key, _index = activate_current_gemini_api_key(logger_override=logger, reason="kira_voice", key_type="text", update_env=False)
            if not key:
                raise RuntimeError("sem chave Gemini de texto")
            try:
                client = genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=30000))  # guardado: solto, fecha no meio
                response = client.models.generate_content(
                    model=os.getenv("KIRA_TTS_MODEL", "gemini-2.5-flash-preview-tts"), contents=text,
                    config=genai_types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=genai_types.SpeechConfig(
                        voice_config=genai_types.VoiceConfig(prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice)))))
                return response.candidates[0].content.parts[0].inline_data.data
            except Exception as exc:
                if not _is_gemini_text_quota_error(exc) or mark_current_gemini_key_quota_exceeded(
                        logger_override=logger, key_type="text", reason=str(exc)) is None:
                    raise
        raise RuntimeError("cota da voz esgotada")

    def _kira_voice_name() -> str:
        # a mesma voz da sessão do Chefe: a do personagem ativo (agent/runtime/voice_persona.py) ou a padrão
        try:
            saved = json.loads((CONFIGS_DIR / "voice_persona.json").read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("voice"):
                return str(saved["voice"])
        except (OSError, ValueError):
            pass
        return os.getenv("GEMINI_REALTIME_VOICE", "Kore") or "Kore"

    # executor próprio: as frases de uma resposta saem em paralelo (a voz leva ~0,75 s por segundo de fala)
    _tts_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="kira-voice")

    async def _guest_tts(text: str, voice: str) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(_tts_executor, lambda: _gemini_tts_sync(text, voice))

    async def _guest_weather(city: str) -> dict:
        return (await weather_provider.forecast(city)).as_dict()

    async def _guest_news(topic: str, interests: list) -> list:
        if topic:
            return await news_provider.search(topic, days=3, limit=12)
        found = await asyncio.gather(*(news_provider.search(item, days=3, limit=4) for item in (interests or [])[:4]), return_exceptions=True)
        items = [row for result in found if isinstance(result, list) for row in result]
        return items or await news_provider.topic_items(None)

    async def _guest_recipe(dish: str) -> dict:
        return await generate_recipe(_kitchen_generate, dish)

    async def _guest_music(query: str) -> dict | None:
        service = _music_component(_music_service, "music_service")
        results = await asyncio.to_thread(service.search_music, query, 5)
        first = next((item for item in results or [] if item.get("videoId")), None)
        return {key: first.get(key) for key in ("videoId", "title", "channel", "thumbnail")} if first else None

    async def _guest_push(title: str, body: str, profile: str) -> None:
        await app.state.web_push.send(title, body, profile=profile, tag="lembrete")

    app.state.guest_reminders = GuestReminders(push=_guest_push)
    app.state.guest_reminders_stop = asyncio.Event()
    app.state.guest_tools = GuestTools(weather=_guest_weather, news=_guest_news, recipe=_guest_recipe, music=_guest_music,
                                       reminders=app.state.guest_reminders, default_city=lambda: "")
    app.include_router(create_guest_router(app.state.guest_chat, profile_of=app.state.remote_settings.profile,
                                           page=str(BASE_DIR / "interface" / "guest.html"), tools=app.state.guest_tools,
                                           voice=KiraVoice(_guest_tts, _kira_voice_name)))
    app.include_router(create_remote_access_router(
        app.state.remote_settings,
        app.state.remote_gateway,
        relay=lambda text, turn: (app.state.devices.note_phone_text(text, turn), _evaluate_js_json(
            f"window.KiraRemoteRelay ? window.KiraRemoteRelay({json.dumps(text)}, {json.dumps(turn)}) : JSON.stringify({{ok: false}})"
        ))[1],
        guest_chat=app.state.guest_chat,
    ))

    async def _phone_voice_join() -> dict:
        """Acesso do celular à sala de voz do PC (participante "Celular"). Sem voz iniciada no PC, o PC inicia (como
        clicar em Iniciar sessão lá) e o celular espera até 80 s."""
        from livekit.api import AccessToken, VideoGrants

        # sala que a tela do PC avisou ao conectar (o "ui_room_connected" do debug é zerado quando a tela prepara a
        # próxima sessão). Sem sala, o PC inicia uma vez e o celular só espera o aviso: perguntar à tela a cada 0,5 s
        # prendia a ponte com a página enquanto ela iniciava a voz, o backend travava e o agente não entrava (2026-10-27).
        room_name = str(getattr(app.state, "pc_room", "") or "")
        if not room_name:
            await asyncio.to_thread(_evaluate_js_json, (
                "(function(){ var live = typeof room !== 'undefined' && room && room.localParticipant;"
                " if (!live && typeof startSession === 'function') startSession();"
                " return JSON.stringify({ok: true}); })()"))
            # 80 s: a primeira conexão do PC com o LiveKit às vezes empaca e a tela tenta de novo sozinha depois de ~50 s
            for _ in range(160):
                await asyncio.sleep(0.5)
                room_name = str(getattr(app.state, "pc_room", "") or "")
                if room_name:
                    break
        if not room_name:
            return {"ok": False, "error": "A voz da Kira não iniciou no PC. Confira se a Kira está aberta lá."}
        credential = _resolve_livekit_credential_for_room(room_name)
        if credential is None:
            return {"ok": False, "error": "Sem LiveKit configurado."}
        if not str(credential.url).startswith("wss://"):
            return {"ok": False, "error": "A voz pelo celular precisa do LiveKit na nuvem (o servidor local não é alcançável do celular)."}
        token = (
            AccessToken(api_key=credential.api_key, api_secret=credential.api_secret)
            .with_identity(PHONE_PARTICIPANT_IDENTITY)
            .with_name("Celular")
            .with_ttl(timedelta(hours=6))
            .with_grants(VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True,
                                     can_publish_data=True, can_update_own_metadata=True))
            .to_jwt()
        )
        _PHONE_VOICE_STATE["hub"] = app.state.ui_actions_hub
        _PHONE_VOICE_STATE["joined_at"] = time.time()
        return {"ok": True, "url": credential.url, "token": token, "room": room_name}

    from services.phone_api import create_phone_router

    from services.phone_rpc import PhoneRpc

    app.state.phone_rpc = PhoneRpc(app.state.ui_actions_hub.publish)
    app.include_router(create_phone_router(app.state.devices, voice_join=_phone_voice_join, push=app.state.web_push, rpc=app.state.phone_rpc))

    async def _browser_control(payload: dict) -> dict:
        """Controle da aba Internet pelo texto digitado (mesmas ações da voz); falha vira ok=False, nunca exceção."""
        try:
            return await live_browser_control(app.state.live_browser, app.state.browser_tasks, payload)
        except Exception as exc:
            logger.warning("CHAT_BROWSER_CONTROL_FAILED action=%s reason=%s", payload.get("action"), type(exc).__name__)
            return {"ok": False}
    from services.gods_eye import GodsEyeData, create_gods_eye_router
    from services.gods_eye_live import Lightning, Ships

    async def _call_globe(method: str, arg: dict) -> object:
        # Olho de Deus aberto NA TV (ou modo "só na TV"): o globo de lá recebe os comandos (focar, camadas, fechar)
        if _tv_screen.SCREEN.godseye and _tv_screen.SCREEN.alive():
            result = await asyncio.to_thread(_tv_screen.SCREEN.call, "godseye", method, arg, 15.0)
            if method == "command" and isinstance(arg, dict) and arg.get("action") == "fechar":
                _tv_screen.SCREEN.godseye = False
            return result
        # Chefe no celular: o globo abre lá (ida e volta pela conexão dos painéis); senão, na tela do PC
        if app.state.devices.active == "celular" and app.state.ui_actions_hub.phone_connected:
            return await app.state.phone_rpc.call("godseye", method, arg)
        js = (f"(function(){{ if (!window.KiraGodsEye) return JSON.stringify({{ok:false,error:'globo_indisponivel'}}); "
              f"return JSON.stringify(window.KiraGodsEye.{method}({json.dumps(arg)})); }})();")
        return await asyncio.to_thread(_evaluate_js_json, js)

    app.include_router(create_gods_eye_router(GodsEyeData(orbits=app.state.sky.tles), token_provider=lambda: KIRA_INTERNAL_TOKEN,
                                              run_js=_evaluate_js_json, call_globe=_call_globe,
                                              live={"raios": Lightning(), "navios": Ships()}))
    from services.security_watch import create_security_router

    app.include_router(create_security_router(token_provider=lambda: KIRA_INTERNAL_TOKEN, run_js=_evaluate_js_json))
    from services.kira_lock import KiraLock, create_lock_router  # tela de bloqueio da Kira (Fase 5)
    from services.kira_lock_guard import LockGuard  # bloqueio forte (Fase 1C): teclado/mouse presos, outros monitores

    kira_lock = KiraLock()
    lock_covers: list = []

    def _main_hwnd() -> int:
        try:
            return int(_webview_win.native.winId()) if _webview_win is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def _lock_screens(on: bool) -> None:
        """Kira em tela cheia no monitor principal e uma tela 'Kira bloqueada' em cada outro monitor (ou desfaz)."""
        if on:  # bloqueio pedido com a janela ainda nascendo (boot): espera ela existir (antes travava tudo)
            from qtpy.QtGui import QGuiApplication
            for _ in range(180):
                if _main_hwnd() and QGuiApplication.primaryScreen() is not None:
                    break
                time.sleep(0.5)
        try:
            _set_main_window_fullscreen(on)
        except Exception:  # noqa: BLE001
            logger.warning("KIRA_LOCK_FULLSCREEN_FAILED", exc_info=True)
        if on:  # por cima de tudo e do monitor principal INTEIRO (a barra de tarefas ficava alcançável)
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                mon = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # monitor principal

                class _MI(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
                info = _MI(cbSize=ctypes.sizeof(_MI))
                user32.GetMonitorInfoW(mon, ctypes.byref(info))
                r = info.rcMonitor
                hwnd = _main_hwnd()
                if hwnd:
                    user32.SetWindowPos(hwnd, -1, r.left, r.top, r.right - r.left, r.bottom - r.top, 0x0040)  # TOPMOST, SHOWWINDOW
            except Exception:  # noqa: BLE001
                logger.warning("KIRA_LOCK_TOPMOST_FAILED", exc_info=True)
        else:
            try:
                import ctypes
                hwnd = _main_hwnd()
                if hwnd:
                    ctypes.windll.user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # NOTOPMOST sem mexer posição/tamanho
            except Exception:  # noqa: BLE001
                pass
        while lock_covers:
            try:
                lock_covers.pop().destroy()
            except Exception:  # noqa: BLE001
                pass
        if not on:
            return
        try:
            import webview
            from qtpy.QtGui import QGuiApplication
            primary = QGuiApplication.primaryScreen().geometry()
            screens = list(webview.screens)  # proxy de lista: chamar dava TypeError e a capa do monitor 2 nunca abria
            for screen in screens or []:
                if (int(screen.x), int(screen.y)) == (primary.x(), primary.y()):
                    continue
                lock_covers.append(webview.create_window(
                    "Kira bloqueada", html=_LOCK_COVER_HTML, screen=screen, x=0, y=0, width=int(screen.width),
                    height=int(screen.height), frameless=True, on_top=True, focus=False, background_color="#05030a"))
        except Exception:  # noqa: BLE001
            logger.warning("KIRA_LOCK_COVERS_FAILED", exc_info=True)
            return
        # O pywebview erra a posição em monitor em pé/com escala (capa nascia deitada e torta): o Windows encaixa cada
        # capa no retângulo exato de um monitor que não é o principal.
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            rects: list = []
            proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            def _mon(_h, _dc, rc, _l):
                r = rc.contents
                if (r.left, r.top) != (0, 0):
                    rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
                return 1
            user32.EnumDisplayMonitors(None, None, proc_t(_mon), 0)
            for _ in range(20):
                found: list = []
                enum_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

                def _win(h, _l):
                    buf = ctypes.create_unicode_buffer(64)
                    user32.GetWindowTextW(h, buf, 64)
                    if buf.value == "Kira bloqueada" and user32.IsWindowVisible(h):
                        found.append(h)
                    return True
                user32.EnumWindows(enum_t(_win), 0)
                if len(found) >= len(rects):
                    break
                time.sleep(0.3)
            for hwnd, (x, y, w, h) in zip(found, rects):
                user32.SetWindowPos(hwnd, -1, x, y, w, h, 0x0040)
            logger.info("KIRA_LOCK_COVERS monitores=%s capas=%s", rects, len(found))
        except Exception:  # noqa: BLE001
            logger.warning("KIRA_LOCK_COVERS_PLACE_FAILED", exc_info=True)

    def _lock_tv(on: bool) -> None:
        try:
            _tv_screen.SCREEN.broadcast({"t": "lock", "on": bool(on)})
        except Exception:  # noqa: BLE001
            pass

    def _lock_released(reason: str) -> None:
        # tela sem sinal / tempo máximo: só solta teclado e mouse; arquivo de destravar: destrava tudo
        if reason == "arquivo_destravar":
            _lock_screens(False)
            _lock_tv(False)
            try:
                _evaluate_js_json("location.reload()")
            except Exception:  # noqa: BLE001
                pass

    lock_guard = LockGuard(kira_lock.flag, _main_hwnd, on_release=_lock_released)

    def _kira_lock_changed(on: bool) -> None:
        if on and lock_guard.active:
            lock_guard.beat()
            return
        if on:
            lock_guard.engage()
        else:
            lock_guard.release("desbloqueada")
        _lock_tv(on)
        threading.Thread(target=_lock_screens, args=(on,), name="kira-lock-screens", daemon=True).start()

    app.include_router(create_lock_router(kira_lock, token_provider=lambda: KIRA_INTERNAL_TOKEN, run_js=_evaluate_js_json,
                                          on_change=_kira_lock_changed))
    # Webcam: olhar agora, vigia (avisa quando aparece alguém) e controle por gestos
    from services.camera_api import create_camera_router, press_media_key
    from services.camera_watch import CameraWatch
    from services.security_watch import RECORDINGS_DIR

    async def _camera_alert(facts: dict, photo: Path) -> None:
        known, unknown = facts.get("conhecidos") or [], facts.get("desconhecidos") or 0
        who = (f" Reconheci: {', '.join(known)}." if known else "") + (" Chegou alguém que eu não conheço." if unknown else "")
        text = f"Vigia: {facts['pessoas']} pessoa(s) na câmera às {facts['hora']}.{who}"
        try:  # vira lembrete de agora: a Kira fala (fora do horário de silêncio) e aparece no sininho
            app.state.reminder_service.create(text, datetime.now().astimezone().isoformat(timespec="seconds"))
        except ValueError as exc:
            logger.info("CAMERA_ALERT_REMINDER_FAILED reason=%s", exc)
        await app.state.notifier.send(f"{text} Foto em Vídeos\\Kira Segurança.", title="Kira · vigia", attach=str(photo))

    async def _camera_gesture(action: str) -> None:
        from services.music_runtime_service import get_music_status

        if get_music_status().get("state") in {"playing", "paused"}:  # música da Kira: o player dela
            js = ("(function(){ if(window.KiraUiControl && window.KiraUiControl.musicCommand) return JSON.stringify("
                  f"window.KiraUiControl.musicCommand({json.dumps(action)})); return JSON.stringify({{ok:false}}); }})();")
            try:
                if (await asyncio.to_thread(_evaluate_js_json, js)).get("ok"):
                    return
            except RuntimeError:
                pass
        await asyncio.to_thread(press_media_key, action)  # YouTube, Spotify etc.: teclas de mídia do Windows

    # Cérebro: memória por significado, documentos do Chefe e núcleo de memória (tudo local)
    from services.brain import Brain
    from services.brain_api import create_brain_router

    app.state.brain = Brain()
    app.include_router(create_brain_router(app.state.brain, token_provider=lambda: KIRA_INTERNAL_TOKEN))
    app.state.camera = CameraWatch(BASE_DIR / "cache" / "vision", RECORDINGS_DIR, on_alert=_camera_alert, on_gesture=_camera_gesture)
    app.include_router(create_camera_router(app.state.camera, publish=app.state.panel_registry.publish, token_provider=lambda: KIRA_INTERNAL_TOKEN))

    @app.middleware("http")
    async def log_api_requests(request: Request, call_next):
        status_poll_path = request.url.path in {
            "/api/system-status",
            "/api/system-vitals",
            "/api/ready",
            "/api/boot/state",
            "/api/ui/panel-visibility/state",
            "/api/vision/status",
            "/api/whatsapp/status",
        }
        if request.url.path.startswith("/api"):
            if status_poll_path and request.method == "GET":
                _rate_limited_log(
                    f"request:{request.method}:{request.url.path}",
                    "[REQUEST] %s %s",
                    request.method,
                    request.url.path,
                    interval_s=30.0,
                )
            else:
                logger.info("[REQUEST] %s %s", request.method, request.url.path)
        started_at = time.monotonic()
        response = await call_next(request)
        if request.headers.get("x-kira-remote") and request.url.path.startswith("/api") and not (status_poll_path and request.method == "GET"):
            from services.channel_logs import phone_log
            phone_log("CEL_PEDIDO", metodo=request.method, rota=request.url.path, status=response.status_code,
                      ms=int((time.monotonic() - started_at) * 1000), papel=request.headers.get("x-kira-role") or "dono")
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        if request.url.path.startswith("/api"):
            if status_poll_path and request.method == "GET" and response.status_code < 400:
                _rate_limited_log(
                    f"response:{request.method}:{request.url.path}:{response.status_code}",
                    "[RESPONSE] %s %s -> %s",
                    request.method,
                    request.url.path,
                    response.status_code,
                    interval_s=30.0,
                )
            else:
                logger.info("[RESPONSE] %s %s -> %s", request.method, request.url.path, response.status_code)
        return response

    @app.on_event("shutdown")
    async def shutdown_event():
        await app.state.window_registry.close_all()
        reminder_service = app.state.reminder_service
        reminder_service.stop()
        reminder_task = app.state.reminder_scheduler_task
        if reminder_task:
            reminder_task.cancel()
            await asyncio.gather(reminder_task, return_exceptions=True)
        app.state.whatsapp_secretary_stop.set()
        app.state.guest_reminders_stop.set()
        if app.state.whatsapp_secretary_task:
            app.state.whatsapp_secretary_task.cancel()
            await asyncio.gather(app.state.whatsapp_secretary_task, return_exceptions=True)
        await app.state.browser_tasks.stop(quiet=True)
        await app.state.live_browser.shutdown()
        if _music_fallback_service is not None:
            _music_fallback_service.stop()
        await asyncio.to_thread(app.state.remote_gateway.stop)
        await app.state.media_search_service.shutdown()
        stop_livekit_server()

    async def _transcription_queue_worker():
        """BLOCO 4 — Worker que consome a fila persistente e publica no bus."""
        queue = get_transcription_queue()
        last_cleanup = 0.0
        while True:
            try:
                events = queue.dequeue_pending(limit=50)
                if events:
                    delivered_ids = []
                    for ev in events:
                        try:
                            await transcription_bus.send_transcription(
                                text=ev.get("text") or "",
                                speaker=ev.get("speaker") or "KIRA",
                                source=ev.get("source") or "queue",
                                final=bool(ev.get("is_final", 1)),
                            )
                            delivered_ids.append(ev["id"])
                        except Exception as _ev_err:
                            logger.warning("[BLOCO4] Falha publicando evento %s: %s", ev.get("id"), _ev_err)
                    if delivered_ids:
                        queue.mark_delivered(delivered_ids)
                now = time.time()
                if now - last_cleanup > 3600:
                    removed = queue.cleanup_old()
                    if removed:
                        logger.info("[BLOCO4] Fila: %d eventos antigos removidos", removed)
                    last_cleanup = now
            except Exception as _worker_err:
                logger.warning("[BLOCO4] Worker da fila falhou: %s", _worker_err)
            await asyncio.sleep(0.5)

    def _runtime_gemini_voice_status(state: dict | None = None, telemetry: dict | None = None) -> dict:
        runtime = dict(state or _runtime_state_snapshot())
        tel = dict(telemetry or _agent_telemetry_snapshot())
        health = dict(tel.get("health") or {})
        key_report = tel.get("keys") or _load_agent_key_report()
        total_keys = 0
        if isinstance(key_report, dict):
            total_keys = int(key_report.get("usable_keys", 0) or key_report.get("configured_keys", 0) or 0)
        live_state = str(
            health.get("kira_realtime_state")
            or health.get("gemini_realtime_state")
            or tel.get("kira_realtime_state")
            or tel.get("gemini_realtime_state")
            or ""
        ).strip().lower()
        voice_degraded = bool(
            health.get("voice_audio_degraded")
            or health.get("voice_degraded")
            or tel.get("voice_audio_degraded")
            or tel.get("voice_degraded")
        )
        live_dead = bool(
            health.get("live_session_dead")
            or tel.get("live_session_dead")
            or live_state in {"failed", "dead", "session_dead"}
        )
        live_connected = bool(health.get("live_connected") or tel.get("live_connected") or runtime.get("gemini_ready"))
        live_ready = bool(total_keys > 0 and live_connected and not voice_degraded and not live_dead)
        if live_dead:
            live_status = "session_dead"
        elif live_ready:
            live_status = "ready"
        elif voice_degraded:
            live_status = "degraded"
        elif total_keys <= 0:
            live_status = "missing_key"
        else:
            live_status = live_state or "not_ready"
        return {
            "gemini": {
                "key_available": bool(total_keys > 0),
                "live_ready": live_ready,
                "live_degraded": bool(total_keys > 0 and not live_ready),
                "live_status": live_status,
                "total_keys": total_keys,
            },
            "voice": {
                "available": bool(live_ready),
                "degraded": bool(voice_degraded or live_dead),
                "text_fallback_available": True,
            },
        }

    @app.on_event("startup")
    async def startup_event():
        global _backend_loop
        _backend_loop = asyncio.get_running_loop()
        try:
            async def _reminder_fired(item) -> None:
                if app.state.kira_on_tv():  # Kira na TV: o lembrete aparece lá também (2026-11-07)
                    _tv_screen.SCREEN.broadcast({"t": "aviso", "title": "Lembrete", "text": str(item.text)[:300]})
                # "me avisa no celular" (ou pedido feito pelo celular): a notificação sai na hora certa, sem esperar a fala
                if item.notify_phone:
                    result = await app.state.notifier.send(item.text, title="Lembrete da Kira", to="celular")
                    logger.info("REMINDER_PHONE_NOTICE sent=%d", len(result.get("sent") or []))

            app.state.reminder_scheduler_task = asyncio.create_task(
                app.state.reminder_service.run(on_fire=_reminder_fired),
                name="kira-reminders-scheduler",
            )
            logger.info("REMINDERS_SCHEDULER_TASK_STARTED")
        except Exception as _reminder_start_error:
            logger.exception("REMINDERS_SCHEDULER_START_FAILED: %s", _reminder_start_error)
        app.state.whatsapp_secretary_task = asyncio.create_task(
            app.state.whatsapp_secretary.run(app.state.whatsapp_secretary_stop),
            name="kira-whatsapp-secretary",
        )
        # lembretes dos perfis comuns do celular (avisam só nos aparelhos de cada um)
        app.state.guest_reminders_task = asyncio.create_task(app.state.guest_reminders.run(app.state.guest_reminders_stop),
                                                             name="kira-guest-reminders")
        # Cérebro: conversas e documentos novos entram no índice sozinhos (começa depois do boot)
        from services.brain_api import index_loop

        app.state.brain_index_task = asyncio.create_task(index_loop(app.state.brain), name="kira-brain-index")
        # acesso pelo celular: só abre a porta se estiver ligado (fora do caminho do boot)
        asyncio.create_task(asyncio.to_thread(app.state.remote_gateway.apply), name="kira-remote-gateway-start")
        app.state.remote_gateway.watch()
        try:
            from services.feature_runtime_registry import initialize_feature_runtime_registry

            initialize_feature_runtime_registry()
        except Exception as _feature_registry_err:
            logger.warning("FEATURE_RUNTIME_REGISTRY_STARTUP_FAILED reason=%s", _feature_registry_err)
        if KIRA_BOOT_MINIMAL:
            _set_whatsapp_runtime_state("disabled", bridge_available=False)
            logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
            logger.info("WHATSAPP_LAZY_READY")
            logger.info("SYSTEM_VITALS_BOOT_SKIPPED reason=boot_minimal")
            logger.info("SYSTEM_VITALS_LAZY_READY")
        else:
            _set_whatsapp_runtime_state("stopped", bridge_available=False)
            logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
            logger.info("WHATSAPP_LAZY_READY")
            logger.info("WHATSAPP_RUNTIME_STATE state=off")
        try:
            asyncio.create_task(_transcription_queue_worker())
            logger.info("[BLOCO4] Worker de fila de transcricao iniciado")
        except Exception as _wq_err:
            logger.warning("[BLOCO4] Falha ao iniciar worker de fila: %s", _wq_err)

    # App da Kira no celular (instalável pelo Chrome): service worker na raiz e manifesto (interface/)
    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(BASE_DIR / "interface" / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    async def app_manifest():
        return FileResponse(BASE_DIR / "interface" / "manifest.webmanifest", media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        satellite = request.query_params.get("role") == "satellite"
        html_path = INTERFACE_DIR / ("satellite.html" if satellite else "index.html")
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @app.get("/avatar", response_class=HTMLResponse)
    async def avatar():
        avatar_path = INTERFACE_DIR / "kira_avatar.html"
        return HTMLResponse(content=avatar_path.read_text(encoding="utf-8"))

    @app.websocket("/ws/transcription")
    async def ws_transcription(websocket: WebSocket):
        await transcription_bus.connect(websocket)
        await transcription_bus.send_to(websocket, {"type": "status", **_runtime_state_snapshot()})
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict) and message.get("type") == "ui_appearance_ack":
                    await transcription_bus.acknowledge_appearance(websocket, message)
        except WebSocketDisconnect:
            await transcription_bus.disconnect(websocket)
        except Exception:
            await transcription_bus.disconnect(websocket)

    @app.get("/livekit/token")
    @app.post("/api/token")
    async def get_token():
        if not LIVEKIT_MANAGER.has_credentials():
            raise HTTPException(status_code=500,
                detail="VariÃ¡veis do .env nÃ£o encontradas.")
        boot_state = boot_orchestrator.get_state()
        if boot_state.get("phase") == BootPhase.FAILED.value:
            raise HTTPException(
                status_code=503,
                detail=str(boot_state.get("error") or "Boot da Kira falhou."),
            )
        current_state = _runtime_state_snapshot()
        agent_minimally_ready, ready_reason = _agent_minimally_ready_for_session_start(current_state)
        if not agent_minimally_ready:
            logger.warning(
                "[SESSION_START_BLOCKED] endpoint=%s room=%s participant=%s reason=%s detail=%s",
                "/api/token",
                "pending_room",
                "usuario",
                ready_reason,
                ready_reason,
            )
            _runtime_log("SESSION_START_BLOCKED", endpoint="/api/token", reason=ready_reason, detail=ready_reason)
            raise HTTPException(
                status_code=503,
                detail="Worker LiveKit/Gemini ainda nao esta pronto.",
            )
        try:
            room_name        = f"kira_room_{random.randint(1000, 9999)}"
            logger.info(
                "[SESSION_START_REQUESTED] endpoint=%s room=%s participant=%s",
                "/api/token",
                room_name,
                "usuario",
            )
            _runtime_log("SESSION_START_REQUESTED", endpoint="/api/token", room=room_name, participant="usuario")
            _reset_session_debug_for_new_attempt(room_name=room_name, participant_name="usuario")
            _update_runtime_readiness(
                requested_room_name=room_name,
                room_name="",
                room_connecting=True,
                room_connected=False,
                room_ready=False,
                gemini_connecting=False,
                gemini_ready=False,
                session_started=False,
                handlers_registered=False,
                voice_pipeline_ready=False,
                runtime_ready=False,
                ui_ready=False,
                backend_ready=bool(current_state.get("backend_ready")),
                agent_process_spawned=bool(current_state.get("agent_process_spawned")),
                agent_connected=bool(current_state.get("agent_connected")),
                agent_ready=bool(current_state.get("agent_ready")),
                startup_greeting_created=False,
                startup_greeting_sanitized=False,
                startup_greeting_enqueued=False,
                startup_greeting_dispatched=False,
                startup_greeting_speaking=False,
                startup_greeting_done=False,
                functional_ready=False,
                boot_recovery_active=False,
                boot_state="session_starting",
                ui_status="connecting",
                event="SESSION_START_REQUESTED",
                message="Preparando sessao LiveKit...",
                error="",
                failure_reason="",
                boot_started_at=time.monotonic(),
            )
            payload = await _create_livekit_session_payload_with_timeout(
                room_name=room_name,
                participant_name="usuario",
            )
            logger.info("[SESSION_TOKEN_ISSUED] endpoint=%s room=%s", "/api/token", room_name)
            _runtime_log("SESSION_TOKEN_ISSUED", endpoint="/api/token", room=room_name)
            logger.info(
                "[SESSION_ROOM_PREPARED] endpoint=%s room=%s prepared=%s",
                "/api/token",
                room_name,
                bool(payload.get("roomPrepared")),
            )
            _runtime_log(
                "SESSION_ROOM_PREPARED",
                endpoint="/api/token",
                room=room_name,
                room_prepared=str(bool(payload.get("roomPrepared"))).lower(),
            )
            logger.info("[SESSION_START_RESPONSE_OK] endpoint=%s room=%s", "/api/token", room_name)
            _runtime_log("SESSION_START_RESPONSE_OK", endpoint="/api/token", room=room_name)
            return JSONResponse(payload)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[SESSION_START] request_failed endpoint=%s room=%s", "/api/token", room_name)
            _runtime_log("AGENT_FAILED", reason=f"dispatch_error:{e}")
            _update_session_debug(
                last_error=str(e),
                last_event="TOKEN_REQUEST_FAILED",
                last_message="Falha ao preparar token da sessao.",
                last_stage="token_failed",
            )
            raise HTTPException(status_code=502, detail=str(e))

    async def _start_session_from_payload(
        payload: dict | None,
        *,
        endpoint_name: str,
    ) -> JSONResponse:
        session_start = time.perf_counter()
        if not LIVEKIT_MANAGER.has_credentials():
            raise HTTPException(status_code=500, detail="Variaveis do .env nao encontradas.")

        boot_state = boot_orchestrator.get_state()
        if boot_state.get("phase") == BootPhase.FAILED.value:
            raise HTTPException(
                status_code=503,
                detail=str(boot_state.get("error") or "Boot da Kira falhou."),
            )

        payload = payload or {}
        room_name = str(payload.get("room_name") or "").strip() or f"kira_room_{random.randint(1000, 9999)}"
        participant_name = str(payload.get("participant_name") or payload.get("participant") or "").strip() or "usuario"
        _session_start_log(
            "SESSION_START_ENTER",
            endpoint=endpoint_name,
            room=room_name,
            participant=participant_name,
        )
        _session_start_log("SESSION_START_ROOM_NAME", room=room_name)
        _session_start_log("SESSION_START_PARTICIPANT", identity=participant_name)
        _session_start_log("SESSION_START_AGENT_NAME", agent_name=AGENT_NAME or "<missing>")
        _session_start_log("SESSION_START_DISPATCH_STRATEGY", strategy="explicit_on_session_start")
        current_state = _runtime_state_snapshot()
        worker_ready = bool(
            current_state.get("agent_worker_ready")
            or current_state.get("worker_ready_received")
            or current_state.get("agent_ready_phase") == "worker_ready"
        )
        livekit_gate_status = str(current_state.get("livekit_status") or "").strip().lower() or "unknown"
        _session_start_log(
            "SESSION_START_GATE_CHECK",
            backend=str(bool(current_state.get("backend_ready"))).lower(),
            livekit=livekit_gate_status,
            worker=str(worker_ready).lower(),
        )
        agent_minimally_ready, ready_reason = _agent_minimally_ready_for_session_start(current_state)
        if not agent_minimally_ready:
            logger.warning(
                "[SESSION_START_BLOCKED] endpoint=%s room=%s participant=%s reason=%s detail=%s",
                endpoint_name,
                room_name,
                participant_name,
                ready_reason,
                ready_reason,
            )
            _session_start_log(
                "SESSION_START_BLOCKED",
                level="warning",
                status=503,
                stage="gate",
                error=ready_reason,
                detail=ready_reason,
            )
            _runtime_log("SESSION_START_BLOCKED", endpoint=endpoint_name, reason=ready_reason, detail=ready_reason)
            raise HTTPException(
                status_code=503,
                detail="Worker LiveKit/Gemini ainda nao esta pronto.",
            )
        _session_start_log("SESSION_START_GATE_ALLOWED", reason=ready_reason)

        logger.info(
            "[SESSION_START_REQUESTED] endpoint=%s room=%s participant=%s",
            endpoint_name,
            room_name,
            participant_name,
        )
        try:
            _runtime_log(
                "SESSION_START_REQUESTED",
                endpoint=endpoint_name,
                room=room_name,
                participant=participant_name,
            )
            _reset_session_debug_for_new_attempt(room_name=room_name, participant_name=participant_name)
            _update_runtime_readiness(
                requested_room_name=room_name,
                room_name="",
                room_connecting=True,
                room_connected=False,
                room_ready=False,
                gemini_connecting=False,
                gemini_ready=False,
                session_started=False,
                handlers_registered=False,
                voice_pipeline_ready=False,
                runtime_ready=False,
                ui_ready=False,
                backend_ready=bool(current_state.get("backend_ready")),
                agent_process_spawned=bool(current_state.get("agent_process_spawned")),
                agent_connected=bool(current_state.get("agent_connected")),
                agent_ready=bool(current_state.get("agent_ready")),
                startup_greeting_created=False,
                startup_greeting_sanitized=False,
                startup_greeting_enqueued=False,
                startup_greeting_dispatched=False,
                startup_greeting_speaking=False,
                startup_greeting_done=False,
                functional_ready=False,
                boot_recovery_active=False,
                boot_state="session_starting",
                ui_status="connecting",
                event="SESSION_START_REQUESTED",
                message="Preparando sessao LiveKit...",
                error="",
                failure_reason="",
                boot_started_at=time.monotonic(),
            )
            prepared = _get_prepared_session()
            if prepared and str(prepared.get("room_name") or prepared.get("roomName") or "") == room_name:
                session_payload = _prepared_session_to_response(prepared)
                _session_start_log("SESSION_START_USING_PREPARED_ROOM", room=room_name)
                _session_start_log("SESSION_START_TOKEN_VALID", room=room_name)
            else:
                if prepared:
                    _session_start_log("SESSION_PREPARE_TOKEN_EXPIRED_RENEWING", room=room_name, reason="room_mismatch_or_expired")
                else:
                    _session_start_log("SESSION_PREPARE_TOKEN_EXPIRED_RENEWING", room=room_name, reason="missing_or_expired")
                session_payload = await _create_livekit_session_payload_with_timeout(
                    room_name=room_name,
                    participant_name=participant_name,
                    create_dispatch=False,
                )
                session_payload = _save_prepared_session(session_payload)
                _session_start_log("SESSION_START_TOKEN_RENEWED", room=room_name)
            timing_id = str(session_payload.get("session_timing_id") or "")
            record_timing(
                event="session_start_token_available",
                timing_id=timing_id,
                source="backend",
                room=room_name,
                endpoint=endpoint_name,
            )
            dispatch_info = session_payload.get("dispatchInfo") or session_payload.get("agentDispatch")
            if isinstance(dispatch_info, dict) and str(dispatch_info.get("id") or "").strip():
                _session_start_log(
                    "SESSION_START_USING_PREPARED_DISPATCH",
                    room=room_name,
                    dispatch_id=str(dispatch_info.get("id") or ""),
                )
                record_timing(
                    event="session_start_dispatch_reused",
                    timing_id=timing_id,
                    source="backend",
                    room=room_name,
                    dispatch_id=str(dispatch_info.get("id") or ""),
                )
            else:
                dispatch_credential = _resolve_livekit_credential_for_room(room_name)
                if dispatch_credential is None:
                    raise RuntimeError("livekit_dispatch_credential_unavailable")
                dispatch_info = await asyncio.wait_for(
                    _ensure_agent_dispatch(room_name, dispatch_credential),
                    timeout=max(float(SELFHOST_DISPATCH_TIMEOUT_SECONDS), 30.0),
                )
                if not isinstance(dispatch_info, dict) or not str(dispatch_info.get("id") or "").strip():
                    raise RuntimeError("livekit_dispatch_not_created")
                _session_start_log("SESSION_START_DISPATCH_ON_DEMAND", room=room_name)
                record_timing(
                    event="session_start_dispatch_created",
                    timing_id=timing_id,
                    source="backend",
                    room=room_name,
                    dispatch_id=str(dispatch_info.get("id") or ""),
                )
            session_payload["dispatchInfo"] = dict(dispatch_info)
            session_payload["agentDispatch"] = dict(dispatch_info)
            session_payload["selfhostDispatchMode"] = "explicit_dispatch"
            _update_session_debug(
                room_name=room_name,
                dispatch_info=dict(dispatch_info),
                selfhost_dispatch_mode="explicit_dispatch",
                last_event="LIVEKIT_DISPATCH_CREATE_OK",
                last_message="Dispatch criado. Aguardando entrada do agente na room.",
                last_stage="dispatch_created",
            )
            logger.info("[SESSION_TOKEN_ISSUED] endpoint=%s room=%s", endpoint_name, room_name)
            _runtime_log("SESSION_TOKEN_ISSUED", endpoint=endpoint_name, room=room_name)
            logger.info(
                "[SESSION_ROOM_PREPARED] endpoint=%s room=%s prepared=%s",
                endpoint_name,
                room_name,
                bool(session_payload.get("roomPrepared")),
            )
            _runtime_log(
                "SESSION_ROOM_PREPARED",
                endpoint=endpoint_name,
                room=room_name,
                room_prepared=str(bool(session_payload.get("roomPrepared"))).lower(),
            )
            response_elapsed = _elapsed_ms(session_start)
            record_timing(
                event="session_start_response_ready",
                timing_id=timing_id,
                source="backend",
                room=room_name,
                elapsed_ms=response_elapsed,
            )
            _session_start_log(
                "SESSION_START_RESPONSE_BEGIN",
                endpoint=endpoint_name,
                room=room_name,
                elapsed_ms=response_elapsed,
            )
            logger.info(
                "[SESSION_START_RESPONSE_OK] endpoint=%s room=%s dispatch_mode=%s room_prepared=%s elapsed_ms=%s",
                endpoint_name,
                room_name,
                session_payload.get("selfhostDispatchMode") or "",
                bool(session_payload.get("roomPrepared")),
                response_elapsed,
            )
            _runtime_log("SESSION_START_RESPONSE_OK", endpoint=endpoint_name, room=room_name, elapsed_ms=response_elapsed)
            if response_elapsed > 3000:
                _session_start_log(
                    "SESSION_START_SLOW",
                    level="warning",
                    elapsed_ms=response_elapsed,
                    slow_stage="session_start_total",
                )
            return JSONResponse(session_payload)
        except HTTPException:
            raise
        except Exception as e:
            _session_start_log(
                "SESSION_START_FAILED",
                level="error",
                status=502,
                stage="unknown",
                error=str(e),
                elapsed_ms=_elapsed_ms(session_start),
            )
            logger.exception("[SESSION_START] request_failed endpoint=%s room=%s", endpoint_name, room_name)
            _runtime_log("AGENT_FAILED", reason=f"dispatch_error:{e}")
            _update_session_debug(
                last_error=str(e),
                last_event="TOKEN_REQUEST_FAILED",
                last_message="Falha ao preparar token da sessao.",
                last_stage="token_failed",
            )
            raise HTTPException(status_code=502, detail=str(e))

    @app.post("/api/livekit/token")
    async def get_livekit_token(payload: dict | None = Body(default=None)):
        return await _start_session_from_payload(payload, endpoint_name="/api/livekit/token")

    @app.get("/api/livekit/token")
    async def get_livekit_token_get(request: Request):
        payload = {
            "room_name": str(request.query_params.get("roomName") or request.query_params.get("room") or "").strip(),
            "participant_name": str(
                request.query_params.get("identity")
                or request.query_params.get("participant")
                or ""
            ).strip(),
        }
        return await _start_session_from_payload(payload, endpoint_name="/api/livekit/token[GET]")

    def _connection_details_envelope(details: dict[str, object]) -> dict[str, object]:
        return {
            "ok": True,
            "success": True,
            "data": details,
            "connectionDetails": details,
            **details,
        }

    @app.get("/api/session/connection-details")
    async def session_connection_details_get(request: Request):
        payload = {
            "room_name": str(request.query_params.get("roomName") or request.query_params.get("room") or "").strip(),
            "participant_name": str(
                request.query_params.get("participantName")
                or request.query_params.get("participant")
                or request.query_params.get("identity")
                or ""
            ).strip(),
        }
        details = await create_session_connection_details(payload)
        return _connection_details_envelope(details)

    @app.post("/api/session/connection-details")
    async def session_connection_details_post(payload: dict | None = Body(default=None)):
        details = await create_session_connection_details(payload)
        return _connection_details_envelope(details)

    async def _prepare_session_for_button_locked(payload: dict | None = None) -> dict[str, object]:
        payload = payload or {}
        state = _runtime_state_snapshot()
        missing: list[str] = []
        if not bool(state.get("backend_ready")):
            missing.append("backend_ready")
        if not bool(state.get("livekit_ready")):
            missing.append("livekit_ready")
        if not bool(state.get("agent_worker_ready") or state.get("agent_ok")):
            missing.append("worker_ready")
        if str(state.get("failure_reason") or state.get("error") or "").strip():
            missing.append("fatal_error")
        if missing:
            logger.info("SESSION_PREPARE_GATE_BLOCKED missing=%s", ",".join(missing))
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error": "core_not_ready",
                    "missing": missing,
                    "block_reason": "Aguardando nucleo minimo da Kira.",
                },
            )

        prepared = _get_prepared_session()
        if prepared:
            prepared = await _maybe_prepare_session_dispatch(prepared, reason="prepared_session_reused")
            logger.info("SESSION_PREPARE_READY_FOR_BUTTON reused=true room=%s", prepared.get("room_name") or prepared.get("roomName"))
            return _prepared_session_to_response(prepared)

        room_name = str(payload.get("room_name") or payload.get("roomName") or "").strip() or f"kira_session_{int(time.time() * 1000)}"
        participant_name = str(payload.get("participant_name") or payload.get("participantName") or payload.get("participant") or "").strip() or "usuario"
        _session_start_log("SESSION_PREPARE_BEGIN", room=room_name, participant=participant_name)
        _session_start_log("SESSION_PREPARE_GATE_ALLOWED", room=room_name)
        session_payload = await _create_livekit_session_payload_with_timeout(
            room_name=room_name,
            participant_name=participant_name,
            create_dispatch=False,
        )
        prepared_payload = _save_prepared_session(session_payload)
        _session_start_log("SESSION_PREPARE_TOKEN_OK", room=room_name)
        prepared_payload = await _maybe_prepare_session_dispatch(prepared_payload, reason="session_prepare")
        _session_start_log("SESSION_PREPARE_READY_FOR_BUTTON", room=room_name)
        return _prepared_session_to_response(prepared_payload)

    async def _prepare_session_for_button(payload: dict | None = None) -> dict[str, object]:
        async with _prepared_session_async_lock:
            return await _prepare_session_for_button_locked(payload)

    async def _maybe_prepare_session_dispatch(prepared: dict[str, object], *, reason: str) -> dict[str, object]:
        """Prepara o dispatch antes do clique quando o modo selecionado permite."""
        if str(os.getenv("KIRA_SESSION_PREPARE_DISPATCH", "1")).strip().lower() in {"0", "false", "no", "off"}:
            return prepared
        existing = prepared.get("dispatchInfo") or prepared.get("agentDispatch")
        if isinstance(existing, dict) and str(existing.get("id") or "").strip():
            return prepared
        room_name = str(prepared.get("room_name") or prepared.get("roomName") or "").strip()
        if not room_name:
            return prepared
        credential = _resolve_livekit_credential_for_room(room_name)
        if credential is None:
            _session_start_log("SESSION_PREPARE_DISPATCH_SKIPPED", level="warning", room=room_name, reason="credential_unavailable")
            return prepared
        dispatch_started = time.perf_counter()
        _session_start_log("SESSION_PREPARE_DISPATCH_BEGIN", room=room_name, reason=reason)
        try:
            dispatch_info = await asyncio.wait_for(
                _ensure_agent_dispatch(room_name, credential),
                timeout=max(float(SELFHOST_DISPATCH_TIMEOUT_SECONDS), 30.0),
            )
            if not isinstance(dispatch_info, dict) or not str(dispatch_info.get("id") or "").strip():
                raise RuntimeError("livekit_dispatch_not_created")
            enriched = dict(prepared)
            enriched.update(
                {
                    "dispatchInfo": dict(dispatch_info),
                    "agentDispatch": dict(dispatch_info),
                    "selfhostDispatchMode": "explicit_dispatch",
                    "dispatch_prepared_at": time.time(),
                }
            )
            saved = _save_prepared_session(enriched)
            _session_start_log(
                "SESSION_PREPARE_DISPATCH_DONE",
                room=room_name,
                dispatch_id=str(dispatch_info.get("id") or ""),
                elapsed_ms=_elapsed_ms(dispatch_started),
            )
            return saved
        except Exception as exc:
            # O token continua valido; o caminho de inicio ainda tentara o
            # dispatch sob demanda como fallback.
            _session_start_log(
                "SESSION_PREPARE_DISPATCH_FAILED",
                level="warning",
                room=room_name,
                reason=reason,
                error=str(exc),
                elapsed_ms=_elapsed_ms(dispatch_started),
            )
            return prepared

    @app.post("/api/system/restart")
    async def system_restart():
        accepted = request_full_restart(reason="startup_mode_changed")
        return {
            "ok": bool(accepted),
            "restart_scheduled": bool(accepted),
            "message": "Reinicializacao agendada." if accepted else "Nao foi possivel agendar a reinicializacao.",
        }

    @app.post("/api/session/timing")
    async def session_timing(payload: dict | None = Body(default=None)):
        data = payload or {}
        timing_id = record_timing(
            event=str(data.get("event") or "frontend_stage"),
            timing_id=str(data.get("timing_id") or ""),
            source="frontend",
            room=str(data.get("room") or data.get("roomName") or ""),
            attempt_id=data.get("attempt_id"),
            elapsed_ms=data.get("elapsed_ms"),
            details=data.get("details") if isinstance(data.get("details"), dict) else {},
        )
        startup_id, startup_elapsed = startup_timing_mark(
            event=str(data.get("event") or "frontend_stage"),
            timing_id=str(data.get("timing_id") or timing_id),
            room=str(data.get("room") or data.get("roomName") or ""),
            source="frontend",
            elapsed_ms=data.get("elapsed_ms"),
            details=data.get("details") if isinstance(data.get("details"), dict) else {},
        )
        return {
            "ok": True,
            "timing_id": startup_id or timing_id,
            "elapsed_ms": startup_elapsed,
        }

    @app.post("/api/session/mic-health")
    async def session_mic_health(payload: dict | None = Body(default=None)):
        data = payload or {}
        snapshot = {
            "room": str(data.get("room") or data.get("roomName") or ""),
            "connected": bool(data.get("connected")),
            "enabled": bool(data.get("enabled")),
            "muted": bool(data.get("muted")),
            "ready_state": str(data.get("ready_state") or ""),
            "track_sid": str(data.get("track_sid") or ""),
            "device_id_present": bool(data.get("device_id_present")),
            "reported_at": time.time(),
        }
        previous = (_session_debug_snapshot().get("ui_mic_health") or {}) if isinstance(_session_debug_snapshot().get("ui_mic_health"), dict) else {}
        changed = {key: snapshot[key] for key in ("enabled", "muted", "ready_state", "track_sid") if previous.get(key) != snapshot[key]}
        if changed:
            # Microfone que "some" (silêncio digital no agente): mostra se foi o botão, o mudo ou a trilha.
            logger.info("UI_MIC_STATE_CHANGED %s", " ".join(f"{key}={value}" for key, value in changed.items()))
        _update_session_debug(ui_mic_health=snapshot, last_event="UI_MIC_HEALTH")
        if snapshot["connected"] and snapshot["enabled"] and snapshot["ready_state"] not in {"", "live"}:
            logger.warning(
                "UI_MIC_HEALTH_DEGRADED room=%s ready_state=%s track_sid=%s",
                snapshot["room"], snapshot["ready_state"], snapshot["track_sid"] or "none",
            )
        return {"ok": True, "health": snapshot}

    @app.post("/api/session/prepare")
    async def prepare_session(payload: dict | None = Body(default=None)):
        return await _prepare_session_for_button(payload)

    def _agent_process_alive_for_session_start() -> bool:
        process = _agent_proc
        try:
            return bool(process is not None and process.poll() is None)
        except Exception:
            return False

    def _spawn_agent_for_session_start_if_needed() -> bool:
        """Recreate the worker after an intentional UI session stop.

        ``/api/session/stop`` is allowed to terminate the LiveKit worker so an
        explicit end-call really releases audio resources.  The next session
        must therefore be able to bootstrap that worker again without requiring
        a second launch of Kira.bat.
        """
        with _session_worker_lifecycle_lock:
            if _agent_process_alive_for_session_start():
                return True
            with _agent_recovery_lock:
                if _agent_process_alive_for_session_start():
                    return True
                logger.warning("SESSION_START_AGENT_RESTART_BEGIN reason=worker_not_alive")
                _runtime_log("SESSION_START_AGENT_RESTART_BEGIN", reason="worker_not_alive")
                try:
                    start_agent()
                except Exception as exc:
                    logger.exception("SESSION_START_AGENT_RESTART_FAILED reason=%s", exc)
                    _runtime_log("SESSION_START_AGENT_RESTART_FAILED", reason=str(exc))
                    return False
                spawned = _agent_process_alive_for_session_start()
                logger.info("SESSION_START_AGENT_RESTART_SPAWNED=%s", str(spawned).lower())
                _runtime_log("SESSION_START_AGENT_RESTART_SPAWNED", spawned=str(spawned).lower())
                return spawned

    async def _ensure_agent_worker_ready_for_session_start() -> tuple[bool, str]:
        """Make a new UI session self-healing after a prior explicit stop."""
        while _session_stop_in_progress.is_set():
            await asyncio.sleep(0.1)
        state = _runtime_state_snapshot()
        worker_ready = bool(
            state.get("agent_worker_ready")
            or state.get("worker_ready_received")
            or state.get("agent_ready_phase") == "worker_ready"
        )
        if _agent_process_alive_for_session_start() and worker_ready:
            return True, "worker_already_ready"

        if not _agent_process_alive_for_session_start():
            if not await asyncio.to_thread(_spawn_agent_for_session_start_if_needed):
                return False, "agent_process_spawn_failed"

        deadline = time.monotonic() + min(max(float(WORKER_HTTP_READY_TIMEOUT_SECONDS), 10.0), 90.0)
        while time.monotonic() < deadline:
            if not _agent_process_alive_for_session_start():
                return False, "agent_process_died_during_restart"
            state = _runtime_state_snapshot()
            worker_ready = bool(
                state.get("agent_worker_ready")
                or state.get("worker_ready_received")
                or state.get("agent_ready_phase") == "worker_ready"
            )
            if worker_ready and not bool(state.get("agent_process_dead")):
                logger.info("SESSION_START_AGENT_READY_AFTER_RESTART")
                _runtime_log("SESSION_START_AGENT_READY_AFTER_RESTART")
                return True, "worker_ready_after_restart"
            await asyncio.sleep(0.25)
        logger.error("SESSION_START_AGENT_READY_TIMEOUT")
        _runtime_log("SESSION_START_AGENT_READY_TIMEOUT")
        return False, "agent_worker_ready_timeout"

    @app.post("/api/session/start")
    async def start_session(payload: dict | None = Body(default=None)):
        logger.info("SESSION_START_USING_CONNECTION_DETAILS source=shared_token_provider_flow")
        worker_ready, worker_reason = await _ensure_agent_worker_ready_for_session_start()
        if not worker_ready:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "worker_not_ready",
                    "reason": worker_reason,
                    "ui_message": "Worker LiveKit/Gemini ainda nao esta pronto.",
                },
            )
        # BLOCO 2 — fonte única da verdade impede race condition
        _rsm = get_runtime_status_manager()
        _snap = _rsm.get_snapshot()
        if not _snap.get("can_start_session", False):
            legacy_state = _runtime_state_snapshot()
            legacy_ready, legacy_reason = _agent_minimally_ready_for_session_start(legacy_state)
            legacy_livekit_ready = str(legacy_state.get("livekit_status") or "").strip().lower() in {"ready", "ok", "connected", "local_ready"}
            legacy_backend_ready = bool(legacy_state.get("backend_ready"))
            if legacy_backend_ready and legacy_livekit_ready and legacy_ready:
                logger.warning(
                    "SESSION_START_RSM_FALLBACK_ALLOWED reason=%s rsm_phase=%s",
                    legacy_reason,
                    _snap.get("phase"),
                )
            else:
                from fastapi import HTTPException
                missing = []
                if not _snap.get("backend_ready"):
                    missing.append("backend")
                if not _snap.get("livekit_ready"):
                    missing.append("livekit")
                if not _snap.get("worker_ready"):
                    missing.append("worker")
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "system_not_ready",
                        "missing": missing,
                        "phase": _snap.get("phase"),
                        "ui_message": _snap.get("ui_message"),
                    },
                )
        return await _start_session_from_payload(payload, endpoint_name="/api/session/start")

    @app.get("/api/boot/state")
    async def boot_state():
        state = _runtime_state_snapshot()
        ttl = 3.0 if state.get("functional_ready") else 2.0
        return _status_response_cache.get_or_compute(
            "GET /api/boot/state",
            ttl,
            _build_boot_state_payload,
        )

    @app.get("/api/boot/debug")
    async def boot_debug():
        """
        Endpoint de debug para inspecionar rapidamente o gating do botao.
        """
        legacy_state = _runtime_state_snapshot()
        augmented = _augment_runtime_state(legacy_state)
        boot_state = boot_orchestrator.get_state()

        return {
            "backend_ready": bool(augmented.get("backend_ready")),
            "backend_ready_source": str(augmented.get("backend_ready_source") or "unknown"),
            "http_server_ready": bool(_server_ready.is_set()),
            "livekit_status": str(augmented.get("livekit_status") or "unknown"),
            "livekit_ready": bool(augmented.get("livekit_status") in {"ready", "ok", "connected", "local_ready"}),
            "livekit_server_ok": bool(augmented.get("livekit_server_ok") or augmented.get("livekit_status") in {"ready", "ok", "connected", "local_ready"}),
            "livekit_worker_boot_status": str(augmented.get("livekit_worker_boot_status") or "unknown"),
            "livekit_ready_source": str(augmented.get("livekit_ready_source") or "unknown"),
            "agent_process_spawned": bool(augmented.get("agent_process_spawned")),
            "agent_connected": bool(augmented.get("agent_connected")),
            "agent_ready": bool(augmented.get("agent_ready")),
            "agent_ok": bool(augmented.get("agent_ready") or (augmented.get("agent_process_spawned") and augmented.get("agent_connected"))),
            "gemini_status": str(augmented.get("gemini_status") or "unknown"),
            "token_status": str(augmented.get("token_status") or "pending"),
            "pre_start_ready": bool(augmented.get("pre_start_ready")),
            "can_start_session": bool(augmented.get("can_start_session")),
            "show_start_button": bool(
                augmented.get("show_start_button") or augmented.get("can_start_session")
            ),
            "block_reason": str(augmented.get("block_reason") or ""),
            "missing_flags": augmented.get("missing_flags", []),
            "secondary_services": {
                "vision": "unavailable" if not bool(legacy_state.get("vision_available", True)) else "ok",
                "whatsapp": str(legacy_state.get("whatsapp_bridge_status") or "unknown"),
                "browser": "ok",
            },
            "boot_orchestrator": boot_state,
        }

    @app.get("/api/maintenance/snapshot")
    async def maintenance_snapshot():
        try:
            return {
                "ok": True,
                "snapshot": collect_maintenance_snapshot(),
            }
        except Exception as exc:
            logger.warning("BLOCO7_MAINTENANCE_SNAPSHOT_FAILED error=%s", exc)
            return {
                "ok": False,
                "error": str(exc),
            }

    @app.get("/api/maintenance/process-health")
    async def maintenance_process_health():
        return _collect_subprocess_health_snapshot()

    @app.get("/api/ready")
    async def ready():
        def _compute_ready_payload():
            boot_state = _build_boot_state_payload()
            state = _runtime_state_snapshot()
            debug = _session_debug_snapshot()
            session_functional_ready = bool(state.get("functional_ready"))
            return {
                "ready": bool(session_functional_ready or boot_state.get("can_start_session")),
                "backend_ready": bool(boot_state.get("backend_ready")),
                "functional_ready": session_functional_ready,
                "phase": boot_state.get("phase"),
                "pre_start_ready": bool(state.get("pre_start_ready")),
                "degraded_start_ready": bool(state.get("degraded_start_ready")),
                "token_issued": bool(state.get("token_issued")),
                "room_prepared": bool(state.get("room_prepared")),
                "room_connected": bool(state.get("room_connected")),
                "room_ready": bool(state.get("room_ready")),
                "gemini_ready": bool(state.get("gemini_ready")),
                "voice_pipeline_ready": bool(state.get("voice_pipeline_ready")),
                "runtime_ready": bool(state.get("runtime_ready")),
                "last_stage": str(debug.get("last_stage") or state.get("boot_state") or ""),
                "last_event": str(debug.get("last_event") or state.get("event") or ""),
                "last_error": str(debug.get("last_error") or state.get("error") or state.get("failure_reason") or ""),
                "boot_error": boot_state.get("boot_error") or "",
                "gemini_status": state.get("gemini_status"),
                "livekit_key": state.get("livekit_key"),
                "livekit_current_slot": state.get("livekit_current_slot"),
                "livekit_total_slots": state.get("livekit_total_slots"),
                "livekit_status": state.get("livekit_status"),
                "token_status": state.get("token_status"),
                "room_status": state.get("room_status"),
                "can_start_session": bool(boot_state.get("can_start_session")),
                "show_start_button": bool(boot_state.get("show_start_button")),
                "block_reason": boot_state.get("block_reason"),
                "missing_flags": state.get("missing_flags") or [],
                "diagnostic_flags": state.get("diagnostic_flags") or [],
                "voice_mode": state.get("voice_mode"),
                "restart_required_for_voice": bool(state.get("restart_required_for_voice")),
                "livekit_message": boot_state.get("livekit_message"),
                "boot": boot_state,
                "state": state,
            }
        return _status_response_cache.get_or_compute("GET /api/ready", 1.5, _compute_ready_payload)

    # --- BLOCO 2: endpoints de runtime status (fonte única da verdade) ---

    @app.get("/api/runtime-status")
    async def api_runtime_status():
        """Snapshot oficial do status da Kira. Fonte única da verdade."""
        legacy = _runtime_state_snapshot()
        return _sync_runtime_status_manager_from_legacy(legacy, already_augmented=True)

    @app.get("/api/livekit/provider/status")
    async def livekit_provider_status():
        return build_livekit_provider_status(selected=get_selected_provider())

    async def _reconcile_agent_standby_after_readerror(control_url: str) -> dict:
        logger.info("ECONOMY_STANDBY_RECONCILE_AFTER_READERROR_BEGIN")
        base_url = control_url.rstrip("/")
        try:
            timeout = _httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0)
            async with _httpx.AsyncClient(timeout=timeout) as client:
                health_response = await client.get(base_url + "/health")
                try:
                    health_payload = health_response.json()
                except Exception:
                    health_payload = {"text": health_response.text[:300]}
                if health_response.status_code >= 400 or not (isinstance(health_payload, dict) and health_payload.get("ok") is not False):
                    reason = f"health_http_{health_response.status_code}"
                    logger.error("ECONOMY_STANDBY_RECONCILE_FAILED reason=%s", reason)
                    return {"ok": False, "reason": reason, "health": health_payload}
                logger.info("ECONOMY_STANDBY_RECONCILE_AGENT_HEALTH_OK")

                status_response = await client.get(base_url + "/status")
                try:
                    status_payload = status_response.json()
                except Exception:
                    status_payload = {"text": status_response.text[:300]}
                if status_response.status_code >= 400 or not isinstance(status_payload, dict):
                    reason = f"status_http_{status_response.status_code}"
                    logger.error("ECONOMY_STANDBY_RECONCILE_FAILED reason=%s", reason)
                    return {"ok": False, "reason": reason, "status": status_payload}

                confirmed = (
                    status_payload.get("state") == "ECONOMY_LOCAL_GEMINI_STANDBY"
                    and status_payload.get("gemini_active") is False
                    and status_payload.get("gemini_ready") is False
                )
                if confirmed:
                    logger.info("ECONOMY_STANDBY_RECONCILE_STATUS_OK")
                    return {
                        "ok": True,
                        "state": "ECONOMY_LOCAL_GEMINI_STANDBY",
                        "gemini_active": False,
                        "gemini_ready": False,
                        "voice_available": False,
                        "reason": "economy_standby",
                        "reconciled_after_read_error": True,
                        "status": status_payload,
                    }
                reason = str(status_payload.get("reason") or status_payload.get("state") or "standby_not_confirmed")
                logger.error("ECONOMY_STANDBY_RECONCILE_FAILED reason=%s", reason)
                return {"ok": False, "reason": reason, "status": status_payload}
        except Exception as exc:
            reason = f"reconcile_unavailable:{exc.__class__.__name__}"
            logger.error("ECONOMY_STANDBY_RECONCILE_FAILED reason=%s", reason)
            return {"ok": False, "reason": reason, "error": str(exc)}

    async def _call_agent_gemini_economy_control(action: str) -> dict:
        control_url = str(os.getenv("KIRA_AGENT_GEMINI_CONTROL_URL", "http://127.0.0.1:8091") or "").strip()
        if not control_url:
            return {"ok": False, "reason": "agent_gemini_control_url_not_configured"}
        action_key = "WAKE_UP" if "wake" in action else "GEMINI_STANDBY"
        is_standby = action_key == "GEMINI_STANDBY"
        logger.info("ECONOMY_%s_AGENT_CONTROL_CHECK_BEGIN", action_key)
        url = control_url.rstrip("/") + f"/{action.lstrip('/')}"
        health_url = control_url.rstrip("/") + "/health"
        last_error: dict | None = None
        for attempt in range(1, 4):
            try:
                timeout = _httpx.Timeout(connect=2.0, read=35.0, write=3.0, pool=2.0)
                async with _httpx.AsyncClient(timeout=timeout) as client:
                    health_response = await client.get(health_url)
                    if health_response.status_code >= 400:
                        reason = f"agent_control_health_http_{health_response.status_code}"
                        logger.error("ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s attempt=%s", action_key, reason, attempt)
                        return {"ok": False, "reason": reason}
                    try:
                        health_payload = health_response.json()
                    except Exception:
                        health_payload = {}
                    if isinstance(health_payload, dict) and health_payload.get("ok") is False:
                        reason = str(health_payload.get("reason") or "agent_control_health_not_ok")
                        logger.error("ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s attempt=%s", action_key, reason, attempt)
                        return {"ok": False, "reason": reason, "health": health_payload}
                    if is_standby:
                        logger.info("ECONOMY_AGENT_CONTROL_STANDBY_HTTP_BEGIN")
                    response = await client.post(url, json={"source": "economy_mode"})
                try:
                    payload = response.json()
                except Exception:
                    payload = {"text": response.text[:500]}
                if is_standby:
                    body_for_log = json.dumps(payload, ensure_ascii=False, default=str)[:500]
                    logger.info(
                        "ECONOMY_AGENT_CONTROL_STANDBY_HTTP_RESPONSE status=%s body=%s",
                        response.status_code,
                        body_for_log,
                    )
                if response.status_code >= 400:
                    reason = f"agent_control_http_{response.status_code}"
                    logger.error("ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s attempt=%s", action_key, reason, attempt)
                    return {"ok": False, "reason": reason, "detail": payload}
                if isinstance(payload, dict):
                    if payload.get("ok") is False:
                        logger.error(
                            "ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s attempt=%s",
                            action_key,
                            payload.get("reason") or "agent_control_not_ok",
                            attempt,
                        )
                    else:
                        logger.info("ECONOMY_%s_AGENT_CONTROL_OK attempt=%s", action_key, attempt)
                    return payload
                logger.info("ECONOMY_%s_AGENT_CONTROL_OK attempt=%s", action_key, attempt)
                return {"ok": True, "detail": payload}
            except _httpx.ReadError as exc:
                reason = f"agent_control_unavailable:{exc.__class__.__name__}"
                last_error = {"ok": False, "reason": reason, "error": str(exc), "attempts": attempt}
                if is_standby:
                    logger.warning("ECONOMY_AGENT_CONTROL_STANDBY_HTTP_READ_ERROR detail=%s", str(exc) or exc.__class__.__name__)
                    reconciled = await _reconcile_agent_standby_after_readerror(control_url)
                    if reconciled.get("ok"):
                        logger.info("ECONOMY_%s_AGENT_CONTROL_OK attempt=%s reconciled=true", action_key, attempt)
                        return reconciled
                    return {
                        "ok": False,
                        "reason": f"{reason}:{reconciled.get('reason') or 'reconcile_failed'}",
                        "error": str(exc),
                        "reconcile": reconciled,
                    }
                logger.warning("ECONOMY_%s_AGENT_CONTROL_RETRY reason=%s attempt=%s", action_key, reason, attempt)
                if attempt < 3:
                    await asyncio.sleep(0.35 * attempt)
                    continue
            except (_httpx.ConnectError, _httpx.ConnectTimeout, _httpx.PoolTimeout, _httpx.ReadTimeout) as exc:
                reason = f"agent_control_unavailable:{exc.__class__.__name__}"
                last_error = {"ok": False, "reason": reason, "error": str(exc), "attempts": attempt}
                if is_standby:
                    logger.warning("ECONOMY_AGENT_CONTROL_STANDBY_HTTP_TIMEOUT detail=%s", str(exc) or exc.__class__.__name__)
                logger.warning("ECONOMY_%s_AGENT_CONTROL_RETRY reason=%s attempt=%s", action_key, reason, attempt)
                if attempt < 3:
                    await asyncio.sleep(0.35 * attempt)
                    continue
            except Exception as exc:
                reason = f"agent_control_unavailable:{exc.__class__.__name__}"
                if is_standby:
                    logger.error("ECONOMY_AGENT_CONTROL_STANDBY_HTTP_FAILED detail=%s", str(exc) or exc.__class__.__name__)
                logger.error("ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s", action_key, reason)
                return {"ok": False, "reason": reason, "error": str(exc)}
        result = last_error or {"ok": False, "reason": "agent_control_unavailable"}
        logger.error("ECONOMY_%s_AGENT_CONTROL_FAILED reason=%s attempts=%s", action_key, result.get("reason"), result.get("attempts", 3))
        return result

    async def _fetch_agent_gemini_control_health() -> dict:
        control_url = str(os.getenv("KIRA_AGENT_GEMINI_CONTROL_URL", "http://127.0.0.1:8091") or "").strip()
        if not control_url:
            logger.info("GEMINI_ECONOMY_CONTROL_NOT_STARTED reason=agent_gemini_control_url_not_configured")
            return {"ok": False, "reason": "agent_gemini_control_url_not_configured"}
        try:
            async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=0.5, read=1.5, write=0.5, pool=0.5)) as client:
                response = await client.get(control_url.rstrip("/") + "/health")
            try:
                payload = response.json()
            except Exception:
                payload = {"text": response.text[:300]}
            if response.status_code >= 400:
                logger.info("GEMINI_ECONOMY_CONTROL_NOT_STARTED reason=health_http_%s", response.status_code)
                return {"ok": False, "reason": f"agent_control_health_http_{response.status_code}", "detail": payload}
            if isinstance(payload, dict):
                if payload.get("ok") is False:
                    logger.info("GEMINI_ECONOMY_CONTROL_NOT_STARTED reason=%s", payload.get("reason") or "health_not_ok")
                else:
                    logger.info("GEMINI_ECONOMY_CONTROL_HEALTH_OK")
                return payload
            logger.info("GEMINI_ECONOMY_CONTROL_HEALTH_OK")
            return {"ok": True, "detail": payload}
        except Exception as exc:
            snapshot = _runtime_state_snapshot()
            reason = f"agent_control_unavailable:{exc.__class__.__name__}"
            logger.info(
                "GEMINI_ECONOMY_CONTROL_NOT_STARTED reason=%s functional_ready=%s room_connected=%s",
                reason,
                str(bool(snapshot.get("functional_ready"))).lower(),
                str(bool(snapshot.get("room_connected"))).lower(),
            )
            return {
                "ok": False,
                "reason": reason,
                "functional_ready": bool(snapshot.get("functional_ready")),
                "room_connected": bool(snapshot.get("room_connected")),
            }

    @app.get("/api/economy/status")
    async def economy_status():
        manager = get_economy_mode_manager()
        runtime_snapshot = _runtime_state_snapshot()
        transition = _reconcile_economy_transport_transition(runtime_snapshot)
        runtime_snapshot = _runtime_state_snapshot()
        status = manager.get_status(runtime_snapshot=runtime_snapshot)
        if (
            bool(status.get("gemini_standby_eligible"))
            and not bool(status.get("economy_standby"))
            and manager.real_gemini_standby_allowed()
            and not str(status.get("last_standby_error") or "").strip()
        ):
            logger.info("ECONOMY_TIMER_REAL_STANDBY_ELIGIBLE")
            logger.info("ECONOMY_TIMER_REAL_STANDBY_BEGIN")
            executor_result = await _call_agent_gemini_economy_control("economy/gemini-standby")
            standby_result = manager.request_gemini_standby(
                runtime_snapshot=runtime_snapshot,
                blockers=_economy_transport_blockers_snapshot(),
                executor=lambda: executor_result,
                source="timer_status",
                force=False,
            )
            if standby_result.get("ok") and standby_result.get("mode") == "real":
                logger.info("ECONOMY_TIMER_REAL_STANDBY_OK")
            elif standby_result.get("mode") == "dry_run":
                logger.info("ECONOMY_TIMER_DRY_RUN_USED reason=%s", standby_result.get("reason") or "unknown")
            else:
                logger.error("ECONOMY_TIMER_REAL_STANDBY_FAILED reason=%s", standby_result.get("reason") or "unknown")
            status = manager.get_status(runtime_snapshot=_runtime_state_snapshot())
        elif bool(status.get("gemini_standby_eligible")) and not manager.real_gemini_standby_allowed():
            logger.info("ECONOMY_TIMER_DRY_RUN_USED reason=%s", manager.real_gemini_standby_block_reason() or "real_flags_disabled")
        status["transport_transition"] = transition
        status["gemini_economy_control_health"] = await _fetch_agent_gemini_control_health()
        return status

    @app.get("/api/economy/config")
    async def economy_config_get():
        return {"ok": True, "config": get_economy_mode_manager().get_config()}

    @app.post("/api/economy/config")
    async def economy_config_set(payload: dict = Body(default={})):
        config = get_economy_mode_manager().update_config(payload or {})
        return {"ok": True, "config": config, "status": get_economy_mode_manager().get_status(runtime_snapshot=_runtime_state_snapshot())}

    @app.post("/api/economy/enable")
    async def economy_enable():
        config = get_economy_mode_manager().enable()
        return {"ok": True, "config": config, "status": get_economy_mode_manager().get_status(runtime_snapshot=_runtime_state_snapshot())}

    @app.post("/api/economy/disable")
    async def economy_disable():
        config = get_economy_mode_manager().disable()
        return {"ok": True, "config": config, "status": get_economy_mode_manager().get_status(runtime_snapshot=_runtime_state_snapshot())}

    @app.post("/api/economy/reset-idle-timer")
    async def economy_reset_idle_timer(payload: dict = Body(default={})):
        reason = str((payload or {}).get("reason") or "api").strip() or "api"
        activity_source = (
            "data_channel_text" if reason == "data_channel_text"
            else "voice_transcript" if reason == "voice_transcript"
            else "wake_text" if reason == "wake_text"
            else "economy_reset_idle"
        )
        result = get_economy_mode_manager().reset_idle_timer(reason=reason)
        return {"ok": True, **result, "status": get_economy_mode_manager().get_status(runtime_snapshot=_runtime_state_snapshot())}

    @app.post("/api/economy/test-wake-word")
    async def economy_test_wake_word(payload: dict = Body(default={})):
        text = str((payload or {}).get("text") or "")
        return {"ok": True, **get_economy_mode_manager().test_wake_word(text)}

    @app.post("/api/economy/gemini-standby")
    async def economy_gemini_standby(payload: dict = Body(default={})):
        manager = get_economy_mode_manager()
        runtime_snapshot = _runtime_state_snapshot()
        blockers = _economy_transport_blockers_snapshot()
        source = str((payload or {}).get("source") or "manual_endpoint").strip() or "manual_endpoint"
        force = bool((payload or {}).get("force", True))
        executor_result = None
        if manager.real_gemini_standby_allowed():
            executor_result = await _call_agent_gemini_economy_control("economy/gemini-standby")
        result = manager.request_gemini_standby(
            runtime_snapshot=runtime_snapshot,
            blockers=blockers,
            executor=(lambda: executor_result) if executor_result is not None else None,
            source=source,
            force=force,
        )
        status = manager.get_status(runtime_snapshot=_runtime_state_snapshot(), blockers=blockers)
        return {"ok": bool(result.get("ok")), **result, "status": status}

    _auto_start_session = {"pending": os.getenv("KIRA_AUTO_START_SESSION") == "1"}

    @app.get("/api/auto-start-session")
    async def auto_start_session_flag():
        """24/7: reaberta por queda ou pelo Windows (Kira.bat) -> a UI clica em INICIAR SESSÃO sozinha (uma vez)."""
        return {"ok": True, "auto": bool(_auto_start_session["pending"])}

    @app.post("/api/auto-start-session/done")
    async def auto_start_session_done():
        _auto_start_session["pending"] = False
        logger.info("AUTO_START_SESSION_CLICKED")
        return {"ok": True}

    @app.get("/api/economy/wake-only")
    async def economy_wake_only_get():
        from services import wake_only_mode
        return {"ok": True, "active": wake_only_mode.is_active()}

    @app.post("/api/economy/wake-only")
    async def economy_wake_only_set(payload: dict = Body(default={})):
        from services import wake_only_mode  # "só responde quando chamada" (o agente lê o mesmo arquivo)
        wake_only_mode.set_active(bool((payload or {}).get("active")), source="ui")
        return {"ok": True, "active": wake_only_mode.is_active()}

    @app.post("/api/economy/gemini-wake")
    async def economy_gemini_wake(payload: dict = Body(default={})):
        manager = get_economy_mode_manager()
        text = str((payload or {}).get("text") or "")
        source = str((payload or {}).get("source") or "manual_endpoint").strip() or "manual_endpoint"
        force = bool((payload or {}).get("force", not text))
        executor_result = None
        if manager.real_gemini_standby_allowed():
            executor_result = await _call_agent_gemini_economy_control("economy/gemini-wake")
        result = manager.request_gemini_wake(
            text=text,
            executor=(lambda: executor_result) if executor_result is not None else None,
            source=source,
            force=force,
        )
        status = manager.get_status(runtime_snapshot=_runtime_state_snapshot())
        return {"ok": bool(result.get("ok")), **result, "status": status}

    @app.post("/api/economy/test-transport-switch")
    async def economy_test_transport_switch(payload: dict = Body(default={})):
        target = str((payload or {}).get("target") or "local").strip().lower()
        manager = get_economy_mode_manager()
        config = manager.get_config()
        runtime_snapshot = _runtime_state_snapshot()
        _reconcile_economy_transport_transition(runtime_snapshot)
        runtime_snapshot = _runtime_state_snapshot()
        blockers = _economy_transport_blockers_snapshot()
        if target == "cloud":
            decision = _economy_transport_switch_manager.can_start_manual_cloud_switch(config, runtime_snapshot, blockers)
        else:
            decision = _economy_transport_switch_manager.can_start_manual_switch(config, runtime_snapshot, blockers)
        return {
            "ok": True,
            "target": target,
            "eligible": bool(decision.should_switch),
            "reason": decision.reason,
            "from_provider": decision.from_provider,
            "to_provider": decision.to_provider,
            "blocked_reasons": transport_block_reasons(config=config, runtime_snapshot=runtime_snapshot, explicit=blockers),
            "cooldown_remaining_seconds": _economy_transport_switch_manager.cooldown_remaining(),
            "status": manager.get_status(runtime_snapshot=runtime_snapshot, blockers=blockers),
        }

    @app.post("/api/economy/switch-to-local")
    async def economy_switch_to_local(payload: dict = Body(default={})):
        source = str((payload or {}).get("source") or "manual_endpoint").strip() or "manual_endpoint"
        return await _execute_economy_transport_switch("local", source=source)

    @app.post("/api/economy/switch-to-cloud")
    async def economy_switch_to_cloud(payload: dict = Body(default={})):
        source = str((payload or {}).get("source") or "manual_endpoint").strip() or "manual_endpoint"
        return await _execute_economy_transport_switch("cloud", source=source)

    @app.get("/api/economy/pending-ui-reconnect")
    async def economy_pending_ui_reconnect():
        transition = _reconcile_economy_transport_transition(_runtime_state_snapshot())
        pending = _get_economy_pending_ui_reconnect()
        if not pending:
            return {"ok": True, "pending": False, "transition": transition}
        return {"ok": True, "pending": True, "transition": transition, **pending}

    @app.post("/api/economy/consume-ui-reconnect")
    async def economy_consume_ui_reconnect(payload: dict = Body(default={})):
        reconnect_id = str((payload or {}).get("id") or "").strip()
        consumed = _consume_economy_pending_ui_reconnect(reconnect_id)
        transition = _reconcile_economy_transport_transition(_runtime_state_snapshot())
        return {"ok": True, "consumed": consumed, "id": reconnect_id, "transition": transition}

    @app.websocket("/ws/runtime-status")
    async def ws_runtime_status(websocket: WebSocket):
        """Stream de status oficial. UI escuta e renderiza sem calcular nada."""
        await websocket.accept()
        manager = get_runtime_status_manager()
        queue = manager.subscribe()
        try:
            while True:
                payload = await queue.get()
                await websocket.send_text(json.dumps(payload))
        except Exception:
            pass
        finally:
            manager.unsubscribe(queue)

    @app.post("/api/runtime/readiness")
    async def api_runtime_readiness(payload: dict = Body(default={})):
        """Recebe eventos de readiness do agente (subprocesso) e atualiza o manager."""
        event = payload.get("event")
        value = bool(payload.get("value", True))
        rsm = get_runtime_status_manager()
        if event == "gemini_ready":
            rsm.set_gemini_ready(value)
        elif event == "voice_pipeline_ready":
            rsm.set_voice_pipeline_ready(value)
        elif event == "functional_ready":
            rsm.set_functional_ready(value)
        elif event == "fatal_error":
            rsm.set_fatal_error(payload.get("message", "unknown"))
        else:
            return {"ok": False, "error": "unknown_event"}
        return {"ok": True, "snapshot": rsm.get_snapshot()}

    # --- fim BLOCO 2 endpoints ---

    @app.post("/api/agent/ready")
    async def agent_ready(request: Request):
        payload: dict = {}
        try:
            if (request.headers.get("content-length") or "0") != "0":
                parsed = await request.json()
                if isinstance(parsed, dict):
                    payload = parsed
        except Exception as exc:
            logger.warning("AGENT_READY_NOTIFY_BODY_IGNORED exc_type=%s exc_repr=%r", exc.__class__.__name__, exc)
        phase = str(payload.get("phase") or "worker_ready").strip().lower() or "worker_ready"
        state = _apply_agent_ready_phase(phase, payload)
        _schedule_transcription_status_push(state)
        status_manager = get_runtime_status_manager()
        if phase in {"ready", "worker_ready", "agent_ready", "backend_ready", "worker_http_ready"}:
            logger.info("WORKER_READY_RECEIVED phase=%s", phase)
            status_manager.set_worker_ready(True)
        elif phase in {"room_connected", "session_room_connected", "room_ready"}:
            status_manager.set_room_ready(True)
        elif phase == "gemini_ready":
            status_manager.set_gemini_ready(True)
        elif phase == "voice_pipeline_ready":
            status_manager.set_voice_pipeline_ready(True)
        elif phase in {"runtime_ready", "session_runtime_ready"}:
            status_manager.set_runtime_ready(True)
        elif phase in {"functional_ready", "ui_ready"}:
            status_manager.set_functional_ready(True)
        return {
            "status": "ok",
            "phase": phase,
            "timestamp": time.time(),
            "state": state,
        }

    @app.get("/api/agent/status")
    async def agent_status():
        boot_state = _build_boot_state_payload()
        state = _runtime_state_snapshot()
        debug = _session_debug_snapshot()
        return {
            "ready": bool(boot_state.get("can_start_session")),
            "backend_ready": bool(boot_state.get("backend_ready")),
            "functional_ready": bool(boot_state.get("functional_ready")),
            "phase": boot_state.get("phase"),
            "pre_start_ready": bool(state.get("pre_start_ready")),
            "degraded_start_ready": bool(state.get("degraded_start_ready")),
            "token_issued": bool(state.get("token_issued")),
            "room_prepared": bool(state.get("room_prepared")),
            "room_connected": bool(state.get("room_connected")),
            "room_ready": bool(state.get("room_ready")),
            "gemini_ready": bool(state.get("gemini_ready")),
            "voice_pipeline_ready": bool(state.get("voice_pipeline_ready")),
            "runtime_ready": bool(state.get("runtime_ready")),
            "last_stage": str(debug.get("last_stage") or state.get("boot_state") or ""),
            "last_event": str(debug.get("last_event") or state.get("event") or ""),
            "last_error": str(debug.get("last_error") or state.get("error") or state.get("failure_reason") or ""),
            "gemini_status": state.get("gemini_status"),
            "livekit_status": state.get("livekit_status"),
            "token_status": state.get("token_status"),
            "room_status": state.get("room_status"),
            "can_start_session": bool(boot_state.get("can_start_session")),
            "show_start_button": bool(boot_state.get("show_start_button")),
            "block_reason": boot_state.get("block_reason"),
            "missing_flags": state.get("missing_flags") or [],
            "diagnostic_flags": state.get("diagnostic_flags") or [],
            "voice_mode": state.get("voice_mode"),
            "restart_required_for_voice": bool(state.get("restart_required_for_voice")),
            "livekit_message": boot_state.get("livekit_message"),
            "timestamp": time.time(),
            "boot": boot_state,
            "state": state,
        }

    @app.get("/api/session/preflight")
    async def session_preflight():
        logger.info("SESSION_PREFLIGHT_READ_ONLY")
        logger.info("SESSION_PREFLIGHT_LIGHTWEIGHT")
        logger.info("SESSION_PREFLIGHT_NO_DISPATCH")
        boot_state = _build_boot_state_payload()
        state = _runtime_state_snapshot()
        debug = _session_debug_snapshot()
        can_start = bool(boot_state.get("can_start_session"))
        degraded_start_ready = bool(state.get("degraded_start_ready"))
        missing_flags = list(boot_state.get("missing_flags") or [])
        block_reason = str(boot_state.get("block_reason") or "").strip()
        if can_start:
            message = "Sistema pronto para iniciar sessao."
        else:
            message = str(block_reason or boot_state.get("livekit_message") or "Sistema ainda inicializando.").strip()
        return {
            "ok": can_start,
            "backend_ready": bool(boot_state.get("backend_ready")),
            "functional_ready": bool(boot_state.get("functional_ready")),
            "phase": boot_state.get("phase"),
            "pre_start_ready": bool(state.get("pre_start_ready")),
            "degraded_start_ready": degraded_start_ready,
            "token_issued": bool(state.get("token_issued")),
            "room_prepared": bool(state.get("room_prepared")),
            "room_connected": bool(state.get("room_connected")),
            "room_ready": bool(state.get("room_ready")),
            "gemini_ready": bool(state.get("gemini_ready")),
            "voice_pipeline_ready": bool(state.get("voice_pipeline_ready")),
            "runtime_ready": bool(state.get("runtime_ready")),
            "last_stage": str(debug.get("last_stage") or state.get("boot_state") or ""),
            "last_event": str(debug.get("last_event") or state.get("event") or ""),
            "last_error": str(debug.get("last_error") or state.get("error") or state.get("failure_reason") or ""),
            "gemini_status": state.get("gemini_status"),
            "livekit_status": state.get("livekit_status"),
            "token_status": state.get("token_status"),
            "room_status": state.get("room_status"),
            "can_start_session": can_start,
            "show_start_button": bool(boot_state.get("show_start_button")),
            "block_reason": block_reason,
            "missing_flags": missing_flags,
            "diagnostic_flags": list(boot_state.get("diagnostic_flags") or []),
            "voice_mode": state.get("voice_mode"),
            "restart_required_for_voice": bool(state.get("restart_required_for_voice")),
            "livekit_message": message,
            "boot": boot_state,
            "state": state,
        }

    @app.get("/api/session/debug")
    async def session_debug():
        debug = _session_debug_snapshot()
        _disp_attempts = debug.get("last_dispatch_attempts", 0)
        _disp_err = debug.get("last_dispatch_error")
        if _disp_err and not debug.get("functional_ready"):
            logger.warning(
                "SYSTEM_STATUS_DISPATCH_FAILURE reason=worker_unavailable room=%s attempts=%s",
                debug.get("room_name") or "none",
                _disp_attempts,
            )
        return {
            "requested_room_name": debug.get("requested_room_name") or "",
            "room_name": debug.get("room_name") or "",
            "prepared_room_name": debug.get("prepared_room_name") or "",
            "participant_name": debug.get("participant_name") or "",
            "token_issued": bool(debug.get("token_issued")),
            "ui_room_connected": bool(debug.get("ui_room_connected")),
            "agent_process_spawned": bool(debug.get("agent_process_spawned")),
            "agent_connected": bool(debug.get("agent_connected")),
            "agent_ready": bool(debug.get("agent_ready")),
            "room_connected": bool(debug.get("room_connected")),
            "room_ready": bool(debug.get("room_ready")),
            "gemini_ready": bool(debug.get("gemini_ready")),
            "voice_pipeline_ready": bool(debug.get("voice_pipeline_ready")),
            "runtime_ready": bool(debug.get("runtime_ready")),
            "functional_ready": bool(debug.get("functional_ready")),
            "livekit_status": debug.get("livekit_status"),
            "token_status": debug.get("token_status"),
            "voice_mode": debug.get("voice_mode"),
            "pre_start_ready": bool(debug.get("pre_start_ready")),
            "selfhost_dispatch_mode": debug.get("selfhost_dispatch_mode") or "unknown",
            "dispatch_info": debug.get("dispatch_info"),
            "room_prepared": bool(debug.get("room_prepared")),
            "last_event": debug.get("last_event") or "",
            "last_message": debug.get("last_message") or "",
            "last_error": debug.get("last_error") or "",
            "last_stage": debug.get("last_stage") or "",
            "ui_room_connected_at": debug.get("ui_room_connected_at") or "",
            "entrypoint_called": debug.get("entrypoint_called", False),
            "last_dispatch_attempts": _disp_attempts,
            "last_dispatch_error": _disp_err,
            "last_dispatch_error_code": debug.get("last_dispatch_error_code"),
            "state": _runtime_state_snapshot(),
        }

    @app.post("/api/session/stop")
    async def session_stop(payload: dict = Body(default={})):
        reason = str(payload.get("reason") or "ui_session_stop").strip() or "ui_session_stop"
        logger.info("SESSION_STOP_REQUESTED reason=%s", reason)
        if reason not in {"ui_disconnect", "participant_disconnect", "room_disconnected", "transport_disconnect"}:
            app.state.pc_room = ""
        transient_disconnect_reasons = {
            "ui_disconnect",
            "participant_disconnect",
            "room_disconnected",
            "transport_disconnect",
        }
        if reason in transient_disconnect_reasons:
            logger.warning(
                "SESSION_STOP_DEFERRED reason=%s action=wait_for_ui_reconnect",
                reason,
            )
            return {
                "ok": True,
                "stopped": False,
                "deferred": True,
                "reason": reason,
                "message": "Desconexao temporaria; sessao preservada durante a janela de reconexao.",
            }

        def _do_session_stop_locked() -> dict:
            logger.info("SESSION_STOP_BEGIN reason=%s", reason)
            # 1) Agente LiveKit (worker na 8081) + filhos registrados no tracker.
            logger.info("SESSION_STOP_AGENT_REQUESTED")
            try:
                stop_agent()
            except Exception as exc:
                logger.warning("SESSION_STOP_AGENT_FAILED_NON_FATAL error=%s", exc)
            # stop_agent ja reseta as flags de runtime/room.
            logger.info("SESSION_STOP_ROOM_CLEANED")

            # 2) LiveKit local (7880) — somente se foi iniciado pela Kira.
            logger.info("SESSION_STOP_PROCESS_CLEANUP_BEGIN livekit_started_here=%s", str(bool(_livekit_started_here)).lower())
            if _livekit_started_here:
                try:
                    stop_livekit_server()
                    logger.info("SESSION_STOP_PROCESS_TERMINATED name=livekit_server port=7880")
                except Exception as exc:
                    logger.warning("SESSION_STOP_LIVEKIT_STOP_FAILED_NON_FATAL error=%s", exc)
            else:
                logger.info("SESSION_STOP_PROCESS_SKIPPED name=livekit_server reason=not_started_by_kira")

            # 3) Verificacao final das portas da Kira. NUNCA matamos processos
            # externos do usuario: apenas reportamos o que ficou vivo e por que.
            time.sleep(0.5)
            port_owners = {3000: "backend_host", 7880: "livekit_server", 8081: "agent_worker"}
            ports_state: dict[str, bool] = {}
            for port, owner in port_owners.items():
                still_open = _transport_port_open(port)
                ports_state[str(port)] = still_open
                if not still_open:
                    logger.info("SESSION_STOP_PROCESS_TERMINATED name=%s port=%s", owner, port)
                elif port == 3000:
                    # O backend e o proprio host deste processo: permanece vivo
                    # de proposito enquanto a janela da Kira estiver aberta.
                    logger.info("SESSION_STOP_PROCESS_LEFT_ALIVE name=%s port=%s reason=backend_host_expected", owner, port)
                else:
                    logger.warning("SESSION_STOP_PROCESS_LEFT_ALIVE name=%s port=%s reason=still_listening", owner, port)
            logger.info("SESSION_STOP_DONE reason=%s", reason)
            return ports_state

        def _do_session_stop() -> dict:
            # Start-up and explicit stop share the worker process.  Keep the
            # lifecycle lock through the port cleanup so a fast second click
            # cannot reuse a worker that is still being terminated.
            with _session_worker_lifecycle_lock:
                return _do_session_stop_locked()

        _session_stop_in_progress.set()
        try:
            ports_state = await asyncio.to_thread(_do_session_stop)
        finally:
            _session_stop_in_progress.clear()
        return {"ok": True, "stopped": True, "reason": reason, "ports": ports_state}

    @app.get("/api/memory/status")
    async def memory_status():
        try:
            from services.memory_provider_manager import get_memory_status

            status = get_memory_status()
            structured = getattr(getattr(app, "state", None), "experience_memory", None)
            if structured is not None:
                try:
                    status["structured"] = structured.summary()
                except Exception as exc:
                    logger.info("STRUCTURED_MEMORY_STATUS_SKIPPED reason=%s", exc.__class__.__name__)
            logger.info("MEMORY_STATUS_REQUESTED provider=%s", status.get("provider") or "unknown")
            return {"success": True, "data": status, "error": None}
        except Exception as exc:
            logger.warning("MEMORY_STATUS_REQUEST_FAILED reason=%s", exc)
            return {
                "success": True,
                "data": {"provider": "unknown", "available": False, "reason": str(exc)},
                "error": None,
            }

    @app.get("/api/sensitive-vault/status")
    async def sensitive_vault_status():
        try:
            from services.sensitive_vault import get_sensitive_vault_status

            status = get_sensitive_vault_status()
            logger.info("SENSITIVE_VAULT_STATUS_REQUESTED active=%s items=%s", str(bool(status.get("active"))).lower(), status.get("items_count") or 0)
            return {"success": True, "data": status, "error": None}
        except Exception as exc:
            logger.warning("SENSITIVE_VAULT_STATUS_REQUEST_FAILED reason=%s", exc.__class__.__name__)
            return {
                "success": True,
                "data": {"active": False, "path_exists": False, "items_count": 0, "error_type": exc.__class__.__name__},
                "error": None,
            }

    @app.get("/api/debug/last-route")
    async def debug_last_route():
        try:
            from services.command_route_debug import get_last_route

            return {"ok": True, "route": get_last_route()}
        except Exception as exc:
            logger.debug("COMMAND_ROUTE_DEBUG_READ_DROPPED error=%s", exc)
            return {"ok": False, "route": {"gates": [], "handled": False}, "error": str(exc)}

    @app.post("/api/session/ui-room-connected")
    async def session_ui_room_connected(payload: dict = Body(default={})):
        room_name = str(payload.get("roomName") or payload.get("room_name") or "").strip()
        attempt_id = str(payload.get("attemptId") or "").strip()
        mic_published = bool(payload.get("mic_published", False))
        participant_identity = str(payload.get("participant_identity") or "").strip()
        audio_track_sid = str(payload.get("audio_track_sid") or "").strip()
        current_state = _runtime_state_snapshot()
        effective_room_name = room_name or str(current_state.get("room_name") or current_state.get("requested_room_name") or "").strip()
        app.state.pc_room = effective_room_name  # o celular entra nesta sala (_phone_voice_join)
        startup_timing_mark(
            event="ui_room_connected",
            room=effective_room_name,
            source="frontend",
            details={"attempt_id": attempt_id},
        )
        if mic_published:
            startup_timing_mark(
                event="mic_published",
                room=effective_room_name,
                source="frontend",
                details={"attempt_id": attempt_id},
            )
        _runtime_log(
            "UI_ROOM_CONNECTED_RECEIVED",
            room=effective_room_name or None,
            attempt=attempt_id or None,
            mic_published=mic_published,
            participant=participant_identity or None,
        )
        logger.info(
            "BOOT_REAL_UI_ROOM_CONNECTED room=%s attempt=%s participant=%s",
            effective_room_name or "unknown",
            attempt_id or "unknown",
            participant_identity or "unknown",
        )
        if mic_published:
            logger.info(
                "BOOT_REAL_MIC_PUBLISHED participant=%s track_sid=%s",
                participant_identity or "unknown",
                audio_track_sid or "unknown",
            )
            logger.info(
                "UI_MIC_PUBLISHED_CONFIRMED participant=%s track_sid=%s",
                participant_identity or "unknown",
                audio_track_sid or "unknown",
            )
        else:
            logger.warning("UI_MIC_NOT_PUBLISHED_YET room=%s", effective_room_name)
        state = _update_runtime_readiness(
            room_name=effective_room_name,
            room_connecting=False,
            room_connected=True,
            event="UI_ROOM_CONNECTED",
            message="UI conectou na room. Aguardando agente e runtime.",
            error="",
            failure_reason="",
        )
        _update_session_debug(
            room_name=effective_room_name or str(state.get("room_name") or ""),
            ui_room_connected=True,
            ui_room_connected_at=_now_iso(),
            ui_mic_published=mic_published,
            ui_participant_identity=participant_identity,
            ui_audio_track_sid=audio_track_sid,
            last_event="UI_ROOM_CONNECTED",
            last_message="UI conectou na room. Aguardando agente e runtime.",
            last_error="",
            last_stage="ui_room_connected",
        )
        _runtime_log(
            "WAITING_AGENT_RUNTIME_READY",
            room=effective_room_name or str(state.get("requested_room_name") or ""),
        )
        # O token pode carregar room_config, mas LiveKit self-host local ainda pode
        # precisar do dispatch explicito depois que a UI entra na room.
        debug_snap = _session_debug_snapshot()
        dispatch_info = debug_snap.get("dispatch_info") or {}
        has_dispatch = bool(
            isinstance(dispatch_info, dict)
            and str(dispatch_info.get("id") or "").strip()
        )
        if has_dispatch:
            logger.info("UI_ROOM_CONNECTED_DISPATCH_ALREADY_ACTIVE room=%s action=none", effective_room_name)
        else:
            logger.warning("UI_ROOM_CONNECTED_WITHOUT_REGISTERED_DISPATCH room=%s action=dispatch_background", effective_room_name)
            _session_start_log(
                "SESSION_DISPATCH_SKIPPED",
                room=effective_room_name,
                reason="no_registered_dispatch_before_ui_connected",
            )
            _dispatch_agent_for_room_background(effective_room_name, reason="ui_room_connected")
        _schedule_transcription_status_push(state)
        logger.info("UI_ROOM_CONNECTED_AGENT_ROOM_READY_PENDING room=%s", effective_room_name)
        return {"status": "ok", "state": _runtime_state_snapshot(), "debug": _session_debug_snapshot()}

    @app.post("/api/agent/error")
    async def agent_error(payload: dict = Body(default={})):
        error_text = str(
            payload.get("error")
            or payload.get("detail")
            or payload.get("message")
            or "Erro crítico no backend."
        ).strip()
        state = _update_runtime_readiness(
            ui_status="error",
            event="AGENT_ERROR",
            error=error_text,
            failure_reason=error_text,
            backend_ready=False,
            functional_ready=False,
            ui_ready=False,
            message=error_text,
        )
        _agent_ready.clear()
        _schedule_transcription_status_push(state)
        return {
            "status": "error_logged",
            "timestamp": time.time(),
            "state": state,
        }

    @app.post("/api/agent/heartbeat")
    async def agent_heartbeat(payload: dict = Body(default={})):
        stage = str(payload.get("stage") or "unknown").strip().lower() or "unknown"
        room_name = str(payload.get("room_name") or "").strip()
        job_id = str(payload.get("job_id") or "").strip()
        iteration = payload.get("iteration", None)
        message_parts = [f"Heartbeat do agent: {stage}"]
        if room_name:
            message_parts.append(f"room={room_name}")
        if job_id:
            message_parts.append(f"job={job_id}")
        if iteration is not None:
            message_parts.append(f"iter={iteration}")
        heartbeat_message = " | ".join(message_parts)
        _update_session_debug(
            room_name=room_name or _session_debug_snapshot().get("room_name") or "",
            last_event=f"AGENT_HEARTBEAT_{stage.upper()}",
            last_message=heartbeat_message,
            last_error="",
            last_stage=stage,
        )
        now_ts = time.time()
        before = _runtime_state_snapshot()
        recovering_from_stale = str(before.get("agent_heartbeat_status") or "").lower() == "stale"
        if recovering_from_stale:
            logger.info("AGENT_HEARTBEAT_RECOVERED stage=%s pid=%s", stage, payload.get("pid", ""))
            logger.info("SESSION_RECOVERY_RECHECK_BEGIN reason=heartbeat_recovered")
        state = _update_runtime_readiness(
            event=f"AGENT_HEARTBEAT_{stage.upper()}",
            message=heartbeat_message,
            last_agent_heartbeat_at=now_ts,
            agent_heartbeat_age_ms=0,
            agent_heartbeat_ok=True,
            agent_heartbeat_status="ok",
            agent_heartbeat_stale_since=0.0,
            agent_status="alive",
            agent_process_dead=False,
            agent_error=False,
            error="",
            failure_reason="",
        )
        logger.info("AGENT_HEARTBEAT_OK stage=%s pid=%s", stage, payload.get("pid", ""))
        if stage == "runtime_alive":  # agente rodando a sessão de verdade (o vigia da sala não derruba)
            _AGENT_RUNTIME_ALIVE_AT[0] = now_ts
        if recovering_from_stale:
            checked = _augment_runtime_state(state)
            logger.info(
                "SESSION_RECOVERY_RECHECK_STATE agent_ok=%s room_ready=%s gemini_ready=%s voice_pipeline_ready=%s runtime_ready=%s functional_ready=%s",
                str(bool(checked.get("agent_ok"))).lower(),
                str(bool(checked.get("room_ready"))).lower(),
                str(bool(checked.get("gemini_ready"))).lower(),
                str(bool(checked.get("voice_pipeline_ready"))).lower(),
                str(bool(checked.get("runtime_ready"))).lower(),
                str(bool(checked.get("functional_ready"))).lower(),
            )
            if checked.get("functional_ready"):
                logger.info("SESSION_RECOVERY_HEARTBEAT_RESTORED")
                logger.info("SESSION_RECOVERY_RUNTIME_RESTORED")
            else:
                logger.info("SESSION_RECOVERY_STILL_DEGRADED reason=critical_runtime_signal_missing")
        _schedule_transcription_status_push(state)
        return {
            "status": "ok",
            "stage": stage,
            "timestamp": time.time(),
            "state": state,
        }

    @app.post("/api/agent/telemetry")
    async def agent_telemetry(payload: dict = Body(default={})):
        try:
            livekit_updates = _extract_livekit_runtime_updates(payload)
            if livekit_updates:
                _update_livekit_runtime_state(
                    livekit_current_slot=livekit_updates.get("livekit_current_slot"),
                    livekit_total_slots=livekit_updates.get("livekit_total_slots"),
                    livekit_status=livekit_updates.get("livekit_status"),
                    voice_mode=livekit_updates.get("voice_mode"),
                    restart_required_for_voice=livekit_updates.get("restart_required_for_voice"),
                    livekit_message=livekit_updates.get("livekit_message"),
                    log_reason="agent_telemetry",
                )
            data = _update_agent_telemetry(payload)
        except Exception as exc:
            logger.warning("[TELEMETRY] falha ao processar (ignorado): %s", exc)
            data = {}
        return {"status": "ok", "timestamp": time.time(), "telemetry": data}

    @app.post("/api/transcription/publish")
    async def publish_transcription(payload: dict = Body(default={})):
        text = str(payload.get("text") or "").strip()
        source = str(payload.get("source") or "system").strip().lower() or "system"
        speaker = str(payload.get("speaker") or "").strip().lower()
        role = str(payload.get("role") or "").strip().lower()
        if role in {"user", "assistant", "system"} and not speaker:
            speaker = "kira" if role == "assistant" else role
        final = bool(payload.get("is_final", payload.get("final", True)))
        reply_id = str(payload.get("reply_id") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()
        turn_id = str(payload.get("turn_id") or "").strip()
        tool_ack = bool(payload.get("tool_ack", False))
        if final and text and getattr(app.state.ui_actions_hub, "phone_connected", False):
            from services.channel_logs import phone_log, preview  # conversa com o celular aberto: vai pro log do celular
            phone_log("CEL_CONVERSA", quem=speaker or role or "?", fonte=source, chars=len(text), turno=turn_id, texto=preview(text))
        logger.info(
            "TRANSCRIPT_PAYLOAD_IDS source=%s reply_id=%s request_id=%s turn_id=%s tool_ack=%s",
            source,
            reply_id or "none",
            request_id or "none",
            turn_id or "none",
            str(tool_ack).lower(),
        )
        if source == "whatsapp_tool" and tool_ack and not (request_id or reply_id):
            assigned_request_id = f"wa-tool-ack-{int(time.time() * 1000)}"
            payload["request_id"] = assigned_request_id
            request_id = assigned_request_id
            logger.info("TRANSCRIPT_TOOL_ACK_ID_ASSIGNED request_id=%s", request_id)

        dedupe_key, key_mode = _build_transcription_dedupe_key(source, text, payload)
        now = time.monotonic()
        existing = _transcription_dedupe_state.get(dedupe_key)
        logger.info("TRANSCRIPT_DEDUPE_CHECK key=%s source=%s final=%s", dedupe_key, source, str(final).lower())
        if existing:
            age_s = float(now - float(existing.get("ts") or now))
            same_text = str(existing.get("text") or "") == text
            old_final = bool(existing.get("final"))
            action = "update"
            if old_final and not final:
                action = "ignore"
                logger.info("TRANSCRIPT_DEDUPE_HIT key=%s action=%s", dedupe_key, action)
                return {"status": "ok", "timestamp": time.time(), "deduped": True, "action": action}
            if final and not old_final:
                logger.info("TRANSCRIPT_FINAL_REPLACES_PARTIAL key=%s", dedupe_key)
            if final and old_final and same_text:
                action = "ignore"
                logger.info("TRANSCRIPT_DEDUPE_HIT key=%s action=%s", dedupe_key, action)
                return {"status": "ok", "timestamp": time.time(), "deduped": True, "action": action}
            if final and old_final and not same_text:
                logger.warning(
                    "TRANSCRIPT_DEDUPE_CONFLICT key=%s old_text=%s new_text=%s",
                    dedupe_key,
                    str(existing.get("text") or "")[:120],
                    text[:120],
                )
            if key_mode != "fingerprint" and age_s <= _TRANSCRIPTION_DEDUPE_WINDOW_SECONDS:
                for evt in _transcription_recent_events:
                    if str(evt.get("source") or "") == source and str(evt.get("key") or "") != dedupe_key:
                        logger.warning(
                            "TRANSCRIPT_POSSIBLE_DUPLICATE_DIFFERENT_IDS source=%s key1=%s key2=%s age_s=%.2f",
                            source,
                            str(evt.get("key") or ""),
                            dedupe_key,
                            age_s,
                        )
            logger.info("TRANSCRIPT_DEDUPE_HIT key=%s action=%s", dedupe_key, action)
        else:
            logger.info("TRANSCRIPT_DEDUPE_MISS key=%s", dedupe_key)

        extra = {
            key: value
            for key, value in dict(payload).items()
            if key not in {"text", "speaker", "source", "final"}
        }
        # Conversa separada por aparelho: o que foi falado no celular aparece no celular, o do PC no PC
        devices = getattr(app.state, "devices", None)
        extra.setdefault("device", devices.active if devices is not None else "pc")
        try:
            await transcription_bus.send_transcription(
                text=text,
                speaker=speaker or None,
                source=source,
                final=final,
                extra=extra,
            )
        except Exception as exc:
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=%s error=%s", source, exc)
            return {"status": "ok", "timestamp": time.time(), "published": False, "non_fatal": True}
        _transcription_dedupe_state[dedupe_key] = {"text": text, "final": final, "ts": now, "source": source}
        _transcription_recent_events.append({"key": dedupe_key, "source": source, "ts": now})
        _transcription_recent_events[:] = [
            item for item in _transcription_recent_events
            if now - float(item.get("ts") or now) <= _TRANSCRIPTION_DEDUPE_WINDOW_SECONDS
        ]
        if source == "system" and "livekit" in text.lower():
            _runtime_log("LIVEKIT_QUOTA_TEXT_ONLY_PUBLISHED_TO_UI", text=text[:160])
        if final and speaker in {"user", "voce", "você", "alan"}:
            _end_topic_if_closing(text)
        return {"status": "ok", "timestamp": time.time()}

    def _end_topic_if_closing(text: str) -> None:
        """'Tá bom, obrigado': o assunto acabou e os cartões dele saem (enquanto a Kira começa o 'de nada')."""
        from services.panel_registry import is_topic_closing

        registry = getattr(app.state, "panel_registry", None)
        if registry is None or not is_topic_closing(text):
            return

        async def close_soon() -> None:
            await asyncio.sleep(1.5)
            closed = await registry.end_topic()
            logger.info("PANELS_TOPIC_CLOSED closed=%d", closed)

        asyncio.ensure_future(close_soon())

    _ui_panel_visibility_state = {
        "version": 0,
        "panels": {
            "system_vitals": False,
            "session_status": True,
            "api_identity": True,
            "transcription": True,
        },
    }

    def _normalize_ui_panel_name(panel: str) -> str:
        raw = str(panel or "").strip()
        normalized = "".join(
            ch for ch in unicodedata.normalize("NFD", raw.lower()) if unicodedata.category(ch) != "Mn"
        )
        normalized = re.sub(r"[\s\-]+", "_", normalized)
        aliases = {
            "all": "all",
            "todos": "all",
            "tudo": "all",
            "todos_os_paineis": "all",
            "todos_os_containers": "all",
            "containers": "all",
            "status": "session_status",
            "status_do_sistema": "session_status",
            "status_sistema": "session_status",
            "system_status": "session_status",
            "sessionstatus": "session_status",
            "session_status": "session_status",
            "sessionStatus": "session_status",
            "api_identity": "api_identity",
            "apiidentity": "api_identity",
            "apis": "api_identity",
            "apis_ativas": "api_identity",
            "api_ativas": "api_identity",
            "api_ativa": "api_identity",
            "painel_apis_ativas": "api_identity",
            "painel_de_apis_ativas": "api_identity",
            "painel_das_apis": "api_identity",
            "livekit_gemini": "api_identity",
            "gemini_livekit": "api_identity",
            "vitals": "system_vitals",
            "systemvitals": "system_vitals",
            "system_vitals": "system_vitals",
            "systemVitals": "system_vitals",
            "sistema_vital": "system_vitals",
            "sistemas_vitais": "system_vitals",
            "transcript": "transcription",
            "transcricao": "transcription",
            "transcription": "transcription",
        }
        return aliases.get(normalized, aliases.get(raw, normalized))

    @app.post("/api/ui/panel-visibility")
    async def ui_panel_visibility(payload: dict = Body(default={})):
        raw_panel = str(payload.get("panel") or "")
        panel = _normalize_ui_panel_name(raw_panel)
        visible = payload.get("visible")
        source = str(payload.get("source") or "api").strip()
        logger.info("UI_PANEL_VISIBILITY_REQUEST panel=%s visible=%s source=%s", raw_panel, visible, source)
        logger.info("UI_PANEL_VISIBILITY_CANONICAL panel=%s", panel)
        if not panel:
            raise HTTPException(status_code=400, detail="Campo 'panel' obrigatorio.")
        if visible is None:
            raise HTTPException(status_code=400, detail="Campo 'visible' obrigatorio.")
        visible = bool(visible)
        allowed_panels = ["system_vitals", "session_status", "api_identity", "transcription", "all"]
        if panel == "all":
            for panel_name in _ui_panel_visibility_state["panels"]:
                _ui_panel_visibility_state["panels"][panel_name] = visible
        elif panel in _ui_panel_visibility_state["panels"]:
            _ui_panel_visibility_state["panels"][panel] = visible
        else:
            logger.warning("UI_PANEL_VISIBILITY_REJECTED reason=unknown_panel panel=%s", panel)
            return {
                "ok": False,
                "error": "unknown_panel",
                "panel": panel,
                "allowed_panels": allowed_panels,
            }
        _ui_panel_visibility_state["version"] = int(_ui_panel_visibility_state["version"]) + 1
        if panel in {"system_vitals", "all"}:
            try:
                from services.system_vitals_runtime_service import set_panel_visible, stop_system_vitals

                if panel == "system_vitals":
                    set_panel_visible(visible, reason="ui_panel_show" if visible else "ui_panel_hide")
                    if visible:
                        logger.info("SYSTEM_VITALS_PANEL_VISIBLE")
                        logger.info("SYSTEM_VITALS_POLLING_START reason=ui_panel_show")
                    else:
                        logger.info("SYSTEM_VITALS_PANEL_HIDDEN")
                        await stop_system_vitals("ui_panel_hide")
                else:
                    set_panel_visible(visible, reason="ui_panel_show_all" if visible else "ui_panel_hide_all")
                    if not visible:
                        logger.info("SYSTEM_VITALS_PANEL_HIDDEN")
                        await stop_system_vitals("ui_panel_hide")
            except Exception as exc:
                logger.warning("SYSTEM_VITALS_PANEL_VISIBILITY_RUNTIME_FAILED reason=%s", exc)
        logger.info(
            "UI_PANEL_VISIBILITY_STATE_UPDATED panel=%s visible=%s version=%s",
            panel,
            str(visible).lower(),
            _ui_panel_visibility_state["version"],
        )
        if panel in {"api_identity", "all"}:
            logger.info(
                "API_PANEL_VISIBILITY_SET visible=%s source=%s",
                str(visible).lower(),
                source or "api",
            )
        await transcription_bus.broadcast({
            "type": "ui_panel_visibility",
            "panel": panel,
            "visible": visible,
            "source": source,
            "version": _ui_panel_visibility_state["version"],
        })
        logger.info(
            "UI_PANEL_VISIBILITY_BROADCAST panel=%s visible=%s version=%s",
            panel,
            str(visible).lower(),
            _ui_panel_visibility_state["version"],
        )
        canonical_panel = panel
        logger.info(
            "UI_PANEL_VISIBILITY_RESPONSE_OK panel=%s visible=%s",
            canonical_panel,
            str(visible).lower(),
        )
        return {
            "ok": True,
            "panel": canonical_panel,
            "visible": visible,
            "version": _ui_panel_visibility_state["version"],
        }

    _PANEL_LAYOUT_KEYS = {"transcript", "apis", "clock", "side"}
    _PANEL_LAYOUT_WHERE = {
        "esquerda", "direita", "centro", "meio", "topo", "cima", "baixo", "padrao",
        "canto superior esquerdo", "canto superior direito", "canto inferior esquerdo", "canto inferior direito",
    }
    _PANEL_LAYOUT_SIZE = {"maior", "menor", "grande", "medio", "pequeno", "padrao"}

    @app.post("/api/ui/panel-layout")
    async def ui_panel_layout(request: Request, payload: dict = Body(default={})):
        """Tool "painel": move ou muda o tamanho de um painel fixo da tela principal (transcrição, APIs, relógio)."""
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        panel = str(payload.get("panel") or "").strip().lower()
        where = str(payload.get("where") or "").strip().lower()
        size = str(payload.get("size") or "").strip().lower()
        if panel not in _PANEL_LAYOUT_KEYS or (where and where not in _PANEL_LAYOUT_WHERE) or (size and size not in _PANEL_LAYOUT_SIZE) or not (where or size):
            return {"ok": False, "error": "invalid", "panels": sorted(_PANEL_LAYOUT_KEYS), "where": sorted(_PANEL_LAYOUT_WHERE), "size": sorted(_PANEL_LAYOUT_SIZE)}
        await transcription_bus.broadcast({"type": "ui_panel_layout", "panel": panel, "where": where, "size": size})
        logger.info("UI_PANEL_LAYOUT panel=%s where=%s size=%s", panel, where or "-", size or "-")
        return {"ok": True}

    _APPEARANCE_CHOICES = {
        "orb": {"nuvem", "colmeia", "voxels", "globo", "thinking", "galaxy", "ring", "particles", "constellation", "hive", "blackhole", "datarings", "plasma", "torus", "ribbons", "fusion"},
        "accent": {"roxo", "ciano", "rosa", "ambar"},
        "bg": {"galaxy", "black", "grid"},
        "clock": {"padrao", "minimo", "neon", "vidro", "terminal", "analogico", "oculto"},
        "menu": {"padrao", "pilula", "trilho", "apps"},
        "chat": {"padrao", "orbe"},
        "player": {"neon", "capsula", "vinil", "onda"},
    }

    @app.post("/api/ui/appearance")
    async def ui_appearance(request: Request, payload: dict = Body(default={})):
        """Tool "aparencia": troca orbe, cor, fundo, relógio, menu e chat na hora (appearance.js na tela)."""
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        patch = {}
        for key, allowed in _APPEARANCE_CHOICES.items():
            value = str(payload.get(key) or "").strip().lower()
            if value:
                if value not in allowed:
                    return {"ok": False, "error": "invalid", "field": key, "allowed": sorted(allowed)}
                patch[key] = value
        if not patch:
            return {"ok": False, "error": "empty", "choices": {key: sorted(value) for key, value in _APPEARANCE_CHOICES.items()}}
        result = await transcription_bus.publish_appearance(patch)
        logger.info(
            "UI_APPEARANCE %s applied=%s delivered=%s reason=%s",
            " ".join(f"{key}={value}" for key, value in patch.items()),
            str(bool(result.get("applied"))).lower(),
            result.get("delivered", 0),
            result.get("reason", "unknown"),
        )
        return {**result, **patch}

    _ACTIVITY_STATES = {"trabalhando", "buscando", "resolvendo", "ouvindo", "conectando", "trancando", "compondo", "respirando", "moldando"}

    @app.post("/api/ui/activity")
    async def ui_activity(request: Request, payload: dict = Body(default={})):
        """Thinking Orbs: o agente avisa o estágio real da tarefa (ferramenta em uso) e as telas mostram o estado."""
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        state = str(payload.get("state") or "")
        if payload.get("done") or state == "pronto":
            await transcription_bus.broadcast({"type": "kira_activity", "state": "pronto", "done": True})
            return {"ok": True}
        if state not in _ACTIVITY_STATES:
            return {"ok": False, "allowed": sorted(_ACTIVITY_STATES)}
        seconds = max(1.0, min(300.0, float(payload.get("seconds") or 12)))
        await transcription_bus.broadcast({"type": "kira_activity", "state": state, "label": str(payload.get("label") or "")[:60], "seconds": seconds})
        return {"ok": True}

    @app.get("/api/ui/panel-visibility/state")
    async def ui_panel_visibility_state():
        return {
            "ok": True,
            "panels": dict(_ui_panel_visibility_state["panels"]),
            "version": _ui_panel_visibility_state["version"],
        }

    @app.post("/api/ui/panel-visibility/state")
    async def set_panel_visibility_state(payload: dict = Body(default={})):
        logger.info("UI_PANEL_LEGACY_ROUTE_BLOCKED route=panel_visibility_state_post reason=handled_by_ui_panel_gate")
        payload = dict(payload or {})
        payload["source"] = payload.get("source") or "legacy_panel_visibility_state"
        result = await ui_panel_visibility(payload)
        return {"success": bool(result.get("ok")), **result}

    def _log_accepted_text_input(text: str) -> None:
        del text
        logger.info("DIALOGUE_LOG_DISABLED reason=user_requested_no_dialogue_file")
        logger.info("DIALOGUE_LOG_WRITER_NOOP active=true")

    def _safe_user_text_preview(text: str, *, max_chars: int = 180) -> str:
        try:
            from services.memory_guard import safe_log_text_preview
            return safe_log_text_preview(text, max_chars=max_chars)
        except Exception:
            return str(text or "").strip()[:max_chars]

    def _chat_send_normalized_text(text: str) -> str:
        normalized = unicodedata.normalize("NFD", str(text or "").strip().lower())
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        normalized = re.sub(r"[^\w\s]", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    def _chat_send_is_incomplete_memory_reference(text: str) -> bool:
        normalized = _chat_send_normalized_text(text)
        return bool(re.match(r"^(lembra|lembrar|recorda|recordar)\s+d[aeo]s?$", normalized))

    def _chat_send_reject_incomplete_memory_reference(*, turn_id: str) -> None:
        rejected_turn_id = str(turn_id or "").strip() or f"chat-send-{int(time.time() * 1000)}"
        try:
            from services.local_command_guard import mark_turn_rejected, should_block_rejected_turn
            mark_turn_rejected(rejected_turn_id, reason="incomplete_memory_reference")
            should_block_rejected_turn(turn_id=rejected_turn_id, source="chat_send")
        except Exception as exc:
            logger.warning("CHAT_SEND_REJECTED_TURN_LOG_FAILED reason=incomplete_memory_reference error=%s", exc)
        logger.info("TURN_REJECTED_SET turn_id=%s reason=incomplete_memory_reference", rejected_turn_id)
        logger.info(
            "AUTO_REPLY_BLOCKED_TURN_REJECTED turn_id=%s source=chat_send reason=incomplete_memory_reference",
            rejected_turn_id,
        )
        logger.info(
            "GEMINI_REPLY_BLOCKED_TURN_REJECTED turn_id=%s source=chat_send reason=incomplete_memory_reference",
            rejected_turn_id,
        )
        logger.info(
            "TEXT_INPUT_LOCAL_COMMAND_EARLY_RETURN reason=incomplete_memory_reference turn_id=%s source=chat_send",
            rejected_turn_id,
        )

    _chat_send_pc_controller = {"value": None}
    _chat_send_volume_context = {"timestamp": 0.0, "action": ""}

    def _chat_send_provider_name() -> str:
        provider = get_selected_provider()
        return str(getattr(provider, "name", "") or "unknown")

    def _chat_send_text_mode(state: dict) -> tuple[bool, str, str, bool]:
        voice_mode = str(state.get("voice_mode") or "voice").strip().lower() or "voice"
        livekit_status = str(state.get("livekit_status") or "unknown").strip().lower() or "unknown"
        voice_ready = bool(
            state.get("functional_ready")
            or (state.get("voice_pipeline_ready") and state.get("room_ready") and state.get("agent_ready"))
        )
        degraded_statuses = {
            "all_failed",
            "auth_failed",
            "quota_exceeded",
            "dns_failed",
            "cert_failed",
            "connect_failed",
            "provider_failed",
            "worker_error",
            "error",
            "failed",
            "unavailable",
        }
        if voice_mode == "text_only":
            return True, "text", "text_only", voice_ready
        if bool(state.get("functional_ready")):
            return True, "text", "functional_ready", voice_ready
        if bool(state.get("backend_ready")) and bool(state.get("agent_ready") or state.get("agent_connected") or state.get("agent_ok")):
            return True, "text", "backend_ready", voice_ready
        livekit_degraded = (
            voice_mode == "unavailable"
            or livekit_status in degraded_statuses
            or bool(state.get("restart_required_for_voice"))
            or bool(state.get("boot_recovery_active"))
            or bool(state.get("agent_error"))
        )
        backend_available = bool(
            state.get("backend_ready")
            or state.get("session_started")
            or state.get("agent_process_spawned")
            or state.get("agent_connected")
        )
        if livekit_degraded and backend_available:
            return True, "fallback", "livekit_degraded", voice_ready
        return False, "blocked", f"voice_mode_{voice_mode}", voice_ready

    def _chat_send_log_session_status(state: dict, *, text_ready: bool, mode: str, reason: str, voice_ready: bool) -> None:
        degraded = mode == "fallback" or str(reason or "") == "livekit_degraded"
        logger.info(
            "SESSION_STATUS provider=%s text_ready=%s voice_ready=%s degraded=%s livekit_status=%s voice_mode=%s",
            _chat_send_provider_name(),
            str(bool(text_ready)).lower(),
            str(bool(voice_ready)).lower(),
            str(bool(degraded)).lower(),
            str(state.get("livekit_status") or "unknown"),
            str(state.get("voice_mode") or "voice"),
        )
        if text_ready:
            logger.info("TEXT_MODE_READY")
            if degraded:
                logger.info("TEXT_FALLBACK_MODE_ENABLED reason=livekit_degraded")
                logger.info("SESSION_DEGRADED_TEXT_AVAILABLE voice_available=%s", str(bool(voice_ready)).lower())
                logger.info("FUNCTIONAL_READY_DEGRADED_LIVEKIT")
                logger.info("TEXT_MODE_RESTORED_AFTER_RESTART")
            else:
                logger.info("FUNCTIONAL_READY_WITH_TEXT_MODE")

    async def _chat_send_publish(
        text: str,
        *,
        source: str,
        speaker: str,
        role: str,
        turn_id: str,
        transcript_kind: str = "",
        phase: str = "",
    ) -> None:
        payload = {
            "text": text,
            "source": source,
            "input_type": "text" if str(source or "").strip().lower() in {"text", "typed_chat", "api_chat_send", "http_text"} else "system",
            "message_type": "text" if str(source or "").strip().lower() in {"text", "typed_chat", "api_chat_send", "http_text"} else "system",
            "speaker": speaker,
            "role": role,
            "final": True,
            "is_final": True,
            "turn_id": turn_id,
            "message_id": f"{turn_id}-{phase or source}-{int(time.time() * 1000)}",
        }
        if transcript_kind:
            payload["transcript_kind"] = transcript_kind
        await publish_transcription(payload)

    async def _chat_send_publish_user(text: str, *, turn_id: str) -> None:
        await _chat_send_publish(text, source="text", speaker="user", role="user", turn_id=turn_id, phase="user")
        logger.info("TRANSCRIPT_USER_PUBLISHED source=text")
        try:
            from services.local_memory_priority import record_local_message

            await asyncio.to_thread(record_local_message, session_id=turn_id, role="user", content=text, source="text")
        except Exception as exc:
            logger.warning("LOCAL_PRIORITY_MESSAGE_FAILED source=text role=user error_type=%s", exc.__class__.__name__)
        try:
            from services.mem0_session_memory import record_memory_turn

            await asyncio.to_thread(record_memory_turn, "user", text, source="text", session_id=turn_id)
        except Exception as exc:
            logger.warning("MEM0_AUTO_SAVE_BUFFER_FAILED source=text role=user error_type=%s", exc.__class__.__name__)

    async def _chat_send_publish_kira(
        text: str,
        *,
        source: str,
        turn_id: str,
        transcript_kind: str = "local_command",
        phase: str = "ack",
    ) -> None:
        response_type = "local_command_ack"
        normalized_source = str(source or "").strip().lower()
        normalized_kind = str(transcript_kind or "").strip().lower()
        if normalized_source == "ui_panel" or normalized_kind == "panel_command":
            response_type = "ui_panel_ack"
        elif normalized_source == "volume_command":
            response_type = "volume_ack"
        elif normalized_source == "browser_command" or normalized_kind == "browser_command":
            response_type = "browser_ack"
        elif normalized_source == "whatsapp_tool":
            if normalized_kind == "whatsapp_status":
                response_type = "whatsapp_status"
            elif normalized_kind == "whatsapp_pending":
                response_type = "clarification_question"
            else:
                response_type = "whatsapp_send_ack"
        elif normalized_source in {"manus", "creation_task"}:
            response_type = "manus_task_ack"
        elif normalized_source == "local_file_command":
            response_type = "local_file_error" if normalized_kind == "local_file_error" else "artifact_ack"
        elif normalized_source in {"artifact_command"}:
            response_type = "artifact_ack"
        styled_text = build_personality_response(
            response_type=response_type,
            raw_text=text,
            source=source,
            should_speak=False,
            text_only=True,
        ).text
        await _chat_send_publish(
            styled_text,
            source=source,
            speaker="kira",
            role="assistant",
            turn_id=turn_id,
            transcript_kind=transcript_kind,
            phase=phase,
        )
        if phase == "final":
            try:
                from services.local_memory_priority import record_local_message

                await asyncio.to_thread(record_local_message, session_id=turn_id, role="assistant", content=styled_text, source="text")
            except Exception as exc:
                logger.warning("LOCAL_PRIORITY_MESSAGE_FAILED source=text role=assistant error_type=%s", exc.__class__.__name__)
            try:
                from services.mem0_session_memory import record_memory_turn

                await asyncio.to_thread(record_memory_turn, "assistant", styled_text, source="text", session_id=turn_id)
            except Exception as exc:
                logger.warning("MEM0_AUTO_SAVE_BUFFER_FAILED source=text role=assistant error_type=%s", exc.__class__.__name__)

    def send_browser_result_to_gemini_context(
        *,
        user_text: str,
        action: str,
        final_text: str,
        result: dict | None = None,
    ) -> str:
        data = result or {}
        url = str(data.get("url") or "").strip()
        title = str(data.get("title") or "").strip()
        summary = str(data.get("summary") or final_text or "").strip()
        page_type = str(data.get("page_type") or "desconhecido").strip()
        quality_reason = str(data.get("quality_reason") or "").strip()
        quality_score = data.get("quality_score")
        screenshot = str(data.get("screenshot") or data.get("last_screenshot") or "").strip()
        screenshot_available = bool(data.get("screenshot_available") or screenshot)
        panel_visible = bool(data.get("browser_panel_visible"))
        screenshot_description = str(data.get("screenshot_description") or "").strip()
        points = data.get("points") or []
        if isinstance(points, (list, tuple)):
            points_text = "; ".join(str(item).strip() for item in points if str(item).strip())
        else:
            points_text = str(points or "").strip()
        fallback_search = str(bool(data.get("fallback_search"))).lower()
        fallback_key = str(bool(data.get("fallback_key"))).lower()
        raw_result = data.get("browser_raw_result") if isinstance(data.get("browser_raw_result"), dict) else {}
        context_parts = [
            "[BROWSER RESULT]",
            f"Usuario pediu: {str(user_text or '').strip()[:500]}",
            f"Tarefa executada: {str(action or '').strip()}",
        ]
        if raw_result:
            context_parts.extend(
                [
                    "[BROWSER_RAW_RESULT]",
                    f"status: {str(raw_result.get('status') or '')}",
                    f"task_type: {str(raw_result.get('task_type') or '')}",
                    f"quality_score: {str(raw_result.get('quality_score') or '')}",
                    f"quality_notes: {str(raw_result.get('quality_notes') or '')[:300]}",
                    f"safe_for_kira_voice_summary: {str(raw_result.get('safe_for_kira_voice_summary') or '')[:900]}",
                    "[/BROWSER_RAW_RESULT]",
                ]
            )
        if url:
            context_parts.append(f"URL aberta: {url[:500]}")
        if title:
            context_parts.append(f"Titulo da pagina: {title[:300]}")
        context_parts.append(f"Tipo da pagina: {page_type}")
        if quality_reason:
            context_parts.append(f"Qualidade do resultado: {quality_reason} score={quality_score}")
        if summary:
            context_parts.append(f"Resumo extraido: {summary[:1800]}")
        if points_text:
            context_parts.append(f"Principais pontos: {points_text[:900]}")
        context_parts.append(f"Imagem/screenshot disponivel: {str(screenshot_available).lower()}")
        context_parts.append(f"A aba do navegador esta visivel ao usuario: {str(panel_visible).lower()}")
        if screenshot:
            context_parts.append(f"Screenshot atual: {screenshot[:500]}")
        if screenshot_description:
            context_parts.append(f"Descricao curta da screenshot: {screenshot_description[:500]}")
        context_parts.extend(
            [
                f"Fallback de busca usado: {fallback_search}",
                f"Fallback de chave Browser usado: {fallback_key}",
                "Instrucao: responda ao usuario em voz, de forma curta e natural, como Kira, em 2 a 5 frases.",
                "Nao leia URL inteira, nao liste botoes/links brutos, nao refaca a pesquisa e nao invente alem do resultado acima.",
                "Se a pagina for ruim, loja, cookie ou seletor de pais, explique isso de forma simples e ofereca procurar outro resultado.",
                "[/BROWSER RESULT]",
            ]
        )
        context = "\n".join(context_parts)
        raw_chars = len(str(data.get("raw_text") or summary or ""))
        logger.info("BROWSER_RAW_CONTEXT_BUILT chars=%s screenshot=%s", raw_chars, str(screenshot_available).lower())
        logger.info("BROWSER_GEMINI_PROMPT_BUILT chars=%s", len(context))
        logger.info("BROWSER_SCREENSHOT_ATTACHED_TO_GEMINI supported=false")
        logger.info("BROWSER_CONTEXT_TO_GEMINI_BEGIN phase=final")
        logger.info("BROWSER_CONTEXT_TO_GEMINI_OK chars=%s", len(context))
        return context

    async def _chat_send_browser_voice(
        message: str,
        *,
        phase: str,
        turn_id: str,
        browser_context: str = "",
        exact_text: bool | None = None,
    ) -> bool:
        try:
            from agent.runtime.turn_coordinator import get_turn_coordinator
            coordinator = get_turn_coordinator()
            pipeline = getattr(coordinator, "_pipeline", None)
            speak_browser_voice = getattr(pipeline, "speak_browser_voice", None)
        except Exception as exc:
            logger.info("BROWSER_VOICE_QUEUE_BLOCKED reason=pipeline_unavailable detail=%s", str(exc)[:120])
            return False
        if not callable(speak_browser_voice):
            logger.info("BROWSER_VOICE_QUEUE_BLOCKED reason=pipeline_unavailable")
            return False
        try:
            resolved_exact_text = bool(phase == "pre_ack") if exact_text is None else bool(exact_text)
            result = await speak_browser_voice(
                text=message,
                phase=phase,
                turn_id=turn_id,
                browser_context=browser_context,
                exact_text=resolved_exact_text,
                timeout_s=36.0,
            )
        except Exception as exc:
            logger.info("BROWSER_VOICE_QUEUE_BLOCKED reason=%s", str(exc)[:160])
            return False
        return bool(isinstance(result, dict) and result.get("delivered"))

    def _chat_send_panel_pre_ack(action: str) -> str:
        if action == "show":
            return build_personality_response(response_type="local_command_pre_ack", raw_text="vou mostrar o painel").text
        if action == "hide":
            return build_personality_response(response_type="local_command_pre_ack", raw_text="vou ocultar o painel").text
        if action == "toggle":
            return build_personality_response(response_type="local_command_pre_ack", raw_text="vou alternar o painel").text
        return build_personality_response(response_type="local_command_pre_ack", raw_text="vou verificar o painel").text

    def _chat_send_panel_already_notice(action: str, panel: str) -> str:
        return format_ui_panel_ack(action=action, panel=panel, already=True).text

    async def _chat_send_handle_panel_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.core.ui_panel_commands import detect_ui_panel_visibility_command
            logger.info('UI_PANEL_GATE_PRECHECK source=text text="%s"', _safe_user_text_preview(text))
            cmd = detect_ui_panel_visibility_command(text)
        except Exception as exc:
            logger.warning("UI_PANEL_COMMAND_GATE_ERROR source=text error=%s", exc)
            return None
        if cmd is None:
            return None
        action = str(cmd.action or "").strip()
        panel = str(cmd.panel or "").strip()
        panels = [str(value).strip() for value in (getattr(cmd, "panels", None) or [panel]) if str(value).strip()]
        visible = action == "show"
        logger.info("UI_PANEL_GATE_MATCHED source=text action=%s panel=%s", action, panel)
        logger.info("LOCAL_COMMAND_TRANSCRIPT_ORDER_BEGIN kind=panel action=%s target=%s", action, panel)
        logger.info("LOCAL_COMMAND_PLAN_CREATED kind=panel action=%s target=%s", action, panel)
        logger.info("PANEL_COMMAND_DETECTED action=%s target=%s", action, panel)
        logger.info("GEMINI_COMMAND_MODE_ENTER kind=panel action=%s target=%s", action, panel)
        logger.info("GEMINI_NORMAL_REPLY_BLOCKED reason=local_command_mode")
        await _chat_send_publish_user(text, turn_id=turn_id)
        await _chat_send_publish_kira(
            _chat_send_panel_pre_ack(action),
            source="ui_panel",
            turn_id=turn_id,
            transcript_kind="panel_command",
            phase="pre",
        )
        logger.info("LOCAL_COMMAND_PRE_ACK_PUBLISHED")
        state_before = dict(_ui_panel_visibility_state.get("panels") or {})
        current_visible: bool | None
        if panel == "all":
            current_visible = all(bool(value) for value in state_before.values()) if state_before else None
        else:
            current_visible = bool(state_before.get(panel)) if panel in state_before else None
        logger.info(
            "PANEL_COMMAND_STATE_BEFORE visible=%s target=%s",
            "unknown" if current_visible is None else str(current_visible).lower(),
            panel,
        )
        if action == "toggle":
            visible = not bool(current_visible if current_visible is not None else True)
        logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_BEGIN kind=panel action=%s target=%s", action, panel)
        logger.info("LOCAL_COMMAND_EXECUTE_BEGIN source=ui_panel action=%s target=%s", action, panel)
        if action in {"show", "hide"} and current_visible is not None and current_visible == visible:
            final_notice = _chat_send_panel_already_notice(action, panel)
            logger.info("PANEL_COMMAND_ALREADY_IN_STATE target=%s visible=%s", panel, str(current_visible).lower())
            logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_OK kind=panel action=%s target=%s already_in_state=true", action, panel)
        else:
            for target_panel in panels:
                logger.info("PANEL_COMMAND_APPLY_BEGIN action=%s target=%s visible=%s", action, target_panel, str(visible).lower())
                logger.info("UI_PANEL_BACKEND_STATE_UPDATE_REQUEST panel=%s visible=%s", target_panel, str(visible).lower())
                api_result = await ui_panel_visibility(
                    {"panel": target_panel, "visible": visible, "source": "ui_panel_command"}
                )
                if not bool(api_result.get("ok", True)):
                    raise RuntimeError(f"panel visibility rejected: {target_panel}")
                logger.info("UI_PANEL_BACKEND_STATE_UPDATE_OK panel=%s visible=%s", target_panel, str(visible).lower())
            final_notice = format_ui_panel_ack(action=action, panel=panel, raw_text=str(cmd.reply or "").strip()).text
            logger.info("UI_PANEL_COMMAND_APPLY_OK action=%s targets=%s", action, ",".join(panels))
            logger.info("PANEL_COMMAND_APPLY_OK action=%s target=%s", action, panel)
            logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_OK kind=panel action=%s target=%s", action, panel)
        logger.info("PANEL_COMMAND_RESULT_CONFIRMED target=%s visible=%s", panel, str(visible).lower())
        logger.info("LOCAL_COMMAND_RESULT_SENT_TO_ASSISTANT")
        logger.info("GEMINI_COMMAND_RESULT_CONTEXT_SENT")
        await _chat_send_publish_kira(final_notice, source="ui_panel", turn_id=turn_id, transcript_kind="panel_command", phase="final")
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("LOCAL_COMMAND_FINAL_ACK_ORDERED_AFTER_RESULT")
        logger.info("UI_PANEL_COMMAND_HANDLED_TEXT_ONLY action=%s targets=%s", action, ",".join(panels))
        logger.info("UI_PANEL_COMMAND_NORMAL_REPLY_BLOCKED action=%s targets=%s", action, ",".join(panels))
        logger.info("LOCAL_COMMAND_EXECUTE_OK source=ui_panel action=%s target=%s", action, panel)
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=ui_panel turn_id=%s", turn_id)
        logger.info("TRANSCRIPTION_ORDER_ENFORCED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {
            "status": "ok",
            "reply": "",
            "handled": True,
            "reason": "ui_panel_command",
            "voice": False,
            "gemini": False,
            "timestamp": time.time(),
        }

    def _chat_send_volume_pre_ack(action: str) -> str:
        if action == "mute":
            return build_personality_response(response_type="local_command_pre_ack", raw_text="vou mutar agora").text
        if action == "unmute":
            return build_personality_response(response_type="local_command_pre_ack", raw_text="vou ativar o som").text
        return build_personality_response(response_type="local_command_pre_ack", raw_text="vou ajustar o volume").text

    def _chat_send_volume_success_notice(action: str, backend_notice: str, value: int | None = None) -> str:
        text_value = str(backend_notice or "").strip()
        match = re.search(r"(\d{1,3})\s*%", text_value)
        percent = int(match.group(1)) if match else value
        return format_volume_ack(action=action, value=percent, raw_text=text_value).text

    def _chat_send_get_pc_controller():
        controller = _chat_send_pc_controller.get("value")
        if controller is not None:
            return controller
        try:
            from agent.pc_control import PCController
        except Exception:
            agent_dir = str(PROJECT_ROOT / "agent")
            if agent_dir not in sys.path:
                sys.path.insert(0, agent_dir)
            from pc_control import PCController

        controller = PCController()
        _chat_send_pc_controller["value"] = controller
        return controller

    async def _chat_send_handle_volume_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.volume_intent_resolver import (
                extract_standalone_volume_percent,
                is_volume_command_candidate,
                parse_volume_intent,
            )
        except Exception as exc:
            logger.warning("VOLUME_RESOLVER_UNAVAILABLE error=%s", exc)
            return None
        now = time.monotonic()
        context_age = now - float(_chat_send_volume_context.get("timestamp") or 0.0)
        recent_context = bool(_chat_send_volume_context.get("action") and context_age <= 12.0)
        standalone_percent = extract_standalone_volume_percent(text)
        if not is_volume_command_candidate(text, has_recent_context=recent_context):
            return None
        await _chat_send_publish_user(text, turn_id=turn_id)
        if standalone_percent is not None and not recent_context:
            logger.info("VOLUME_STANDALONE_PERCENT_UI_ALLOWED percent=%s", standalone_percent)
        intent = parse_volume_intent(text)
        if intent.intent == "unknown":
            return None
        if standalone_percent is not None:
            logger.info("VOLUME_PERCENT_CONTINUATION_MATCHED percent=%s age_ms=%s", standalone_percent, int(context_age * 1000))
        action_for_log = {
            "set_volume_absolute": "set",
            "adjust_volume_relative": "increase" if intent.direction == "up" else "decrease",
            "mute_volume": "mute",
            "unmute_volume": "unmute",
        }.get(intent.intent, intent.intent)
        _chat_send_volume_context.update({"timestamp": now, "action": intent.intent})
        logger.info("VOLUME_CONTEXT_STORED action=%s ttl_seconds=12", intent.intent)
        logger.info("VOLUME_COMMAND_MATCHED source=text intent=%s", intent.intent)
        logger.info("LOCAL_COMMAND_TRANSCRIPT_ORDER_BEGIN kind=volume action=%s target=system_volume", action_for_log)
        logger.info("LOCAL_COMMAND_PLAN_CREATED kind=volume action=%s target=system_volume", action_for_log)
        logger.info("GEMINI_COMMAND_MODE_ENTER kind=volume action=%s target=system_volume", action_for_log)
        logger.info("GEMINI_NORMAL_REPLY_BLOCKED reason=local_command_mode")
        logger.info("VOLUME_COMMAND_DETECTED action=%s", action_for_log)
        logger.info(
            'VOLUME_COMMAND_MATCHED text="%s" action=%s value=%s',
            text[:180],
            action_for_log,
            intent.value if intent.value is not None else (abs(int(intent.delta or 0)) or ""),
        )
        await _chat_send_publish_kira(
            _chat_send_volume_pre_ack(action_for_log),
            source="volume_command",
            turn_id=turn_id,
            phase="pre",
        )
        logger.info("LOCAL_COMMAND_PRE_ACK_PUBLISHED")
        controller = _chat_send_get_pc_controller()
        logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_BEGIN kind=volume action=%s target=system_volume", action_for_log)
        logger.info("LOCAL_COMMAND_EXECUTE_BEGIN source=volume_command action=%s", intent.intent)
        logger.info("VOLUME_APPLY_BEGIN target=%s", intent.value if intent.value is not None else action_for_log)
        try:
            if intent.intent == "set_volume_absolute" and intent.value is not None:
                logger.info("VOLUME_COMMAND_SET_PERCENT percent=%s", intent.value)
                notice = await asyncio.to_thread(controller.set_volume_absolute, int(intent.value))
            elif intent.intent == "adjust_volume_relative" and intent.direction:
                logger.info(
                    "VOLUME_COMMAND_STEP_%s delta=%s",
                    "INCREASE" if intent.direction == "up" else "DECREASE",
                    intent.delta if intent.delta is not None else (10 if intent.direction == "up" else -10),
                )
                notice = await asyncio.to_thread(controller.adjust_volume_relative, intent.direction, intent.step or "default")
            elif intent.intent == "mute_volume":
                notice = await asyncio.to_thread(controller.mute_volume)
            elif intent.intent == "unmute_volume":
                notice = await asyncio.to_thread(controller.unmute_volume)
            else:
                return None
            final_notice = _chat_send_volume_success_notice(action_for_log, notice, intent.value)
            logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_OK kind=volume action=%s", action_for_log)
            logger.info("VOLUME_APPLY_OK value=%s", intent.value if intent.value is not None else action_for_log)
        except Exception as exc:
            logger.warning("VOLUME_BACKEND_UNAVAILABLE error=%s", exc)
            logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_FAILED reason=%s", exc)
            logger.info("LOCAL_COMMAND_EXECUTE_FAILED source=volume_command error=%s", exc)
            final_notice = format_volume_ack(action=action_for_log, raw_text="nao consegui alterar o volume pelo backend atual", ok=False).text
        logger.info("VOLUME_RESULT_CONFIRMED")
        logger.info("LOCAL_COMMAND_RESULT_SENT_TO_ASSISTANT")
        logger.info("GEMINI_COMMAND_RESULT_CONTEXT_SENT")
        await _chat_send_publish_kira(final_notice, source="volume_command", turn_id=turn_id, phase="final")
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("LOCAL_COMMAND_FINAL_ACK_ORDERED_AFTER_RESULT")
        logger.info("GEMINI_ROUTE_BLOCKED reason=volume_command")
        logger.info("VOICE_ROUTE_BLOCKED reason=volume_command")
        logger.info("VOLUME_COMMAND_HANDLED_TEXT_ONLY intent=%s", intent.intent)
        logger.info("VOLUME_COMMAND_NORMAL_REPLY_BLOCKED intent=%s", intent.intent)
        logger.info("LOCAL_COMMAND_EXECUTE_OK source=volume_command action=%s", intent.intent)
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=volume_command turn_id=%s", turn_id)
        logger.info("TRANSCRIPTION_ORDER_ENFORCED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {"status": "ok", "reply": "", "handled": True, "reason": "volume_command", "voice": False, "gemini": False, "timestamp": time.time()}

    async def _chat_send_handle_local_file_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.core.local_file_command_gate import detect_local_file_command, execute_local_file_command
        except Exception as exc:
            logger.warning("LOCAL_FILE_COMMAND_RESOLVER_UNAVAILABLE error=%s", exc)
            return None

        cmd = detect_local_file_command(text)
        if cmd is None:
            return None

        await _chat_send_publish_user(text, turn_id=turn_id)
        logger.info("LOCAL_FILE_COMMAND_DETECTED action=%s target=%s", cmd.action, cmd.target_kind)
        logger.info("LOCAL_COMMAND_TRANSCRIPT_ORDER_BEGIN kind=local_file action=%s target=%s", cmd.action, cmd.target_kind)
        logger.info("LOCAL_COMMAND_PLAN_CREATED kind=local_file action=%s target=%s", cmd.action, cmd.target_kind)
        logger.info("GEMINI_COMMAND_MODE_ENTER kind=local_file action=%s target=%s", cmd.action, cmd.target_kind)
        logger.info("GEMINI_NORMAL_REPLY_BLOCKED reason=local_file_command")
        logger.info("BROWSER_ROUTE_BLOCKED reason=local_file_command")
        try:
            result = await asyncio.to_thread(execute_local_file_command, cmd)
        except Exception as exc:
            logger.warning("LOCAL_FILE_COMMAND_EXECUTE_FAILED error=%s", exc)
            result = {"handled": True, "ok": False, "notice": "Interface: nao consegui verificar essa pasta.", "path": ""}

        ok = bool(result.get("ok", False))
        notice = str(result.get("notice") or "Interface: nao encontrei essa pasta.").strip()
        await _chat_send_publish_kira(
            notice,
            source="local_file_command",
            turn_id=turn_id,
            transcript_kind="local_file_command" if ok else "local_file_error",
            phase="final",
        )
        try:
            from services.local_command_guard import mark_local_command
            mark_local_command("local_file_command", seconds=5.0)
        except Exception:
            pass
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("GEMINI_ROUTE_BLOCKED reason=local_file_command")
        logger.info("VOICE_ROUTE_BLOCKED reason=local_file_command")
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=local_file_command turn_id=%s", turn_id)
        logger.info("TRANSCRIPTION_ORDER_ENFORCED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {"status": "ok", "reply": "", "handled": True, "reason": "local_file_command", "voice": False, "gemini": False, "timestamp": time.time()}

    async def _chat_send_handle_artifact_command(text: str, *, turn_id: str) -> dict | None:
        """Artifact Command Gate para texto: 'cade o ultimo arquivo/PDF/TXT', abrir
        arquivo/pasta e destino pendente. Roda antes do Gemini/chat generico.
        Responde text-only; nunca chama Manus/voz/generate_reply/site."""
        try:
            from agent.core.artifact_commands import handle_artifact_command
        except Exception as exc:
            logger.warning("ARTIFACT_COMMAND_RESOLVER_UNAVAILABLE error=%s", exc)
            return None

        logger.info("ARTIFACT_ROUTING_CHECK text=%r", _safe_user_text_preview(text))
        # handle_artifact_command resolve pending + detecta + executa (open file/folder).
        # Sem match, retorna handled=False sem efeitos colaterais.
        try:
            result = await asyncio.to_thread(handle_artifact_command, text)
        except Exception as exc:
            logger.warning("ARTIFACT_COMMAND_EXECUTE_FAILED error=%s", exc)
            return None
        if not isinstance(result, dict) or not bool(result.get("handled")):
            logger.info("ARTIFACT_ROUTING_NOT_MATCHED")
            return None

        action = str(result.get("action") or "artifact")
        logger.info("ARTIFACT_ROUTING_MATCHED action=%s", action)
        logger.info("ARTIFACT_COMMAND_BLOCKED_GEMINI_FALLTHROUGH action=%s", action)
        logger.info("GEMINI_ROUTE_BLOCKED reason=artifact_command")
        logger.info("VOICE_ROUTE_BLOCKED reason=artifact_command")

        await _chat_send_publish_user(text, turn_id=turn_id)
        response = str(result.get("response") or "").strip()
        if response.startswith("Interface:"):
            response = response[len("Interface:"):].lstrip()
        if not response:
            response = "Nao encontrei um artifact valido recente."
        await _chat_send_publish_kira(
            response,
            source="artifact_command",
            turn_id=turn_id,
            transcript_kind="artifact_command",
            phase="final",
        )
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=artifact_command turn_id=%s", turn_id)
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {
            "status": "ok",
            "reply": response,
            "handled": True,
            "reason": "artifact_command",
            "action": action,
            "kind": result.get("kind"),
            "path": result.get("path"),
            "voice": False,
            "gemini": False,
            "timestamp": time.time(),
        }

    async def _chat_send_handle_creation_task_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.core.creation_task_commands import build_creation_task_ack, detect_creation_task_command
        except Exception as exc:
            logger.warning("CREATION_TASK_RESOLVER_UNAVAILABLE error=%s", exc)
            return None

        cmd = detect_creation_task_command(text)
        if cmd is None:
            return None

        await _chat_send_publish_user(text, turn_id=turn_id)
        logger.info("CREATION_TASK_TEXT_ROUTE_MATCHED kind=%s", cmd.task_type)
        logger.info("CREATION_TASK_GEMINI_DISABLED")
        logger.info("CREATION_TASK_VOICE_DISABLED")
        logger.info("GEMINI_ROUTE_BLOCKED reason=creation_task")
        try:
            from services.artifact_registry import create_creation_job
            from services.manus_runtime_service import finalize_manus_task, submit_manus_task

            job = create_creation_job(cmd, "text", text)
            job_id = str(job.get("job_id") or "")
            submit_result = await submit_manus_task(job)
            accepted = bool(getattr(submit_result, "accepted", False))

            async def _completion_notice(payload: dict) -> None:
                message = str(payload.get("message") or "").strip()
                if message:
                    await _chat_send_publish_kira(
                        message,
                        source="manus",
                        turn_id=turn_id,
                        transcript_kind="creation_task",
                        phase="final",
                    )

            if accepted:
                asyncio.create_task(finalize_manus_task(job, submit_result, completion_notifier=_completion_notice))
            ack = build_creation_task_ack(
                cmd.task_type,
                "text",
                command=cmd,
                job_id=job_id,
                executor_available=accepted,
                unavailable_reason=str(getattr(submit_result, "reason", "") or "manus_unavailable"),
            )
            notice = str(ack.get("panel_text") or "").strip()
        except Exception as exc:
            logger.warning("CREATION_TASK_TEXT_ROUTE_FAILED error=%s", exc)
            ack = build_creation_task_ack(
                cmd.task_type,
                "text",
                command=cmd,
                executor_available=False,
                unavailable_reason=str(exc) or "manus_unavailable",
            )
            notice = str(ack.get("panel_text") or "O Manus nao esta disponivel agora. A tarefa nao foi enviada.").strip()

        await _chat_send_publish_kira(
            notice,
            source="creation_task",
            turn_id=turn_id,
            transcript_kind="creation_task",
            phase="final",
        )
        try:
            from services.local_command_guard import mark_local_command
            mark_local_command("creation_task", seconds=5.0)
        except Exception:
            pass
        logger.info("CREATION_TASK_NO_SPEECH_REQUEST_CREATED")
        logger.info("CREATION_TASK_NO_AUDIO_CONFIRMATION")
        logger.info("CREATION_TASK_EARLY_RETURN")
        logger.info("LOCAL_COMMAND_HANDLED_EARLY_RETURN source=creation_task")
        logger.info("NORMAL_REPLY_BLOCKED_BY_LOCAL_COMMAND source=creation_task")
        logger.info("GHOST_REPLY_BLOCKED_BY_LOCAL_COMMAND source=creation_task")
        return {"status": "ok", "reply": "", "handled": True, "reason": "creation_task", "voice": False, "gemini": False, "timestamp": time.time()}

    def _chat_send_is_whatsapp_status_query(text: str) -> bool:
        normalized = unicodedata.normalize("NFD", str(text or "").strip().lower())
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        normalized = re.sub(r"[^\w\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not re.search(r"\b(whatsapp|zap|wpp|whats)\b", normalized):
            return False
        channel = r"(?:whatsapp|zap|wpp|whats)"
        status_word = r"(?:status|situacao|funcionando|conectad[oa]?|ligad[oa]?|online|logad[oa]?|pront[oa]?|abert[oa]?|ativo|rodando|mandou|enviou|deu certo|foi|pendente|pendentes)"
        status_patterns = (
            rf"\b(?:status|situacao)\b.*\b{channel}\b",
            rf"\b{channel}\b.*\b{status_word}\b",
            rf"\b(?:verifica|verifique|confere|confira|checa|cheque|consulta|consulte)\b.*\b{channel}\b",
            rf"\b(?:como)\b.*\b(?:esta|ta|vai|anda)\b.*\b{channel}\b",
            rf"\b{channel}\b.*\b(?:esta|ta)\b.*\b{status_word}\b",
        )
        return any(re.search(pattern, normalized) for pattern in status_patterns)

    # ---- Navegador da Kira pelo texto digitado (mesmo fluxo do dispatcher de voz) --
    async def _browser_agent_publish(text: str, response: str, *, turn_id: str) -> dict:
        # HOTFIX 3.3: redige e-mail/telefone na transcricao publica (arquivos de auditoria).
        # O valor real ja foi usado pelo Browser Agent antes daqui.
        # FASE 0 — o redator browser_agent.redact foi apagado junto com o Browser Agent
        # legado. Sem redacao dedicada disponivel; publica o texto cru (este caminho legado
        # ja nao recebe segredos).
        safe_text = str(text or "")
        await _chat_send_publish_user(safe_text, turn_id=turn_id)
        logger.info("GEMINI_ROUTE_BLOCKED reason=browser_agent_command")
        logger.info("VOICE_ROUTE_BLOCKED reason=browser_agent_command")
        await _chat_send_publish_kira(response, source="browser_command", turn_id=turn_id,
                                      transcript_kind="browser_command", phase="final")
        # PROBLEMA B — a pergunta/resposta do Browser Agent TAMBEM e falada pela Kira
        # (nao so texto na UI), pois o usuario pode estar com a UI minimizada. Vale
        # para tarefas iniciadas por texto; o caminho de voz ja fala via _emit_browser_response.
        try:
            spoke = await _chat_send_browser_voice(response, phase="final", turn_id=turn_id)
            logger.info("BROWSER_FOLLOWUP_VOICE_REQUESTED spoke=%s", str(bool(spoke)).lower())
        except Exception as _bv_exc:
            logger.info("BROWSER_FOLLOWUP_VOICE_SKIPPED reason=%s", str(_bv_exc)[:120])
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {"status": "ok", "reply": response, "handled": True, "reason": "browser_agent_command",
                "voice": False, "gemini": False, "timestamp": time.time()}

    def _open_browser_tab_ui() -> dict:
        """Abre a aba Internet na UI (mesmo open_tab do dispatcher de voz) para a aba
        mostrar o screenshot/stream do navegador."""
        js = ("(function(){ if(window.KiraUiControl && window.KiraUiControl.openTab)"
              " return JSON.stringify(window.KiraUiControl.openTab('browser'));"
              " return JSON.stringify({ok:false,error:'bridge_not_available'}); })();")
        return _evaluate_js_json(js)

    async def _chat_send_browser_control(text: str, *, turn_id: str) -> dict:
        """FASE 4 (texto) — controle da aba aberta via /control (teclado/scroll/back/close)
        ou Vision. Espelha o _browser_handle_control do dispatcher de voz."""
        import unicodedata as _ud
        try:
            from agent.core.browser_agent_commands import _parse_youtube_control as _pyc
        except Exception:
            _pyc = None
        n = " ".join(str(text or "").lower().split())
        n = "".join(c for c in _ud.normalize("NFD", n) if _ud.category(c) != "Mn")
        # fechar a aba/navegador
        if re.search(r"\b(fecha|fechar|encerra|encerrar)\b", n):
            await _browser_control({"action": "close"})
            # fecha a aba Internet na UI (volta pra Kira) -> para o poll de screenshot.
            try:
                _evaluate_js_json(
                    "(function(){ if(window.KiraUiControl && window.KiraUiControl.openTab)"
                    " return JSON.stringify(window.KiraUiControl.openTab('kira'));"
                    " return JSON.stringify({ok:false}); })();"
                )
            except Exception:
                pass
            return await _browser_agent_publish(text, "Fechei o navegador, chefe.", turn_id=turn_id)
        # clicar: posição -> clique determinístico no YouTube (click_index); título -> Vision.
        if re.search(r"\b(clica|clique|clicar|seleciona|selecionar|escolhe|escolher)\b", n) \
                or re.search(r"\b(o\s+\d+|primeir|segund|terceir|quart|quint)", n):
            instr = (f"Na pagina atual, clique no que o usuario pediu: '{text}'. "
                     "Se for posicao (primeiro/segundo/o N), clique no N-esimo resultado/link principal.")
            pos = None
            for _k, _v in (("primeir", 1), ("segund", 2), ("terceir", 3), ("quart", 4), ("quint", 5),
                           ("sext", 6), ("setim", 7), ("oitav", 8), ("non", 9), ("decim", 10)):
                if _k in n:
                    pos = _v
                    break
            if pos is None:
                _md = re.search(r"\b(?:o|no|n[uú]mero)\s+(\d+)\b", n) or re.search(r"\b(\d+)\b", n)
                if _md:
                    try:
                        pos = int(_md.group(1))
                    except Exception:
                        pos = None
            if pos is not None:
                data = await _browser_control({"action": "click_index", "n": pos})
            else:
                data = await _browser_control({"action": "vision_click", "instruction": instr})
            ok = bool(data.get("ok"))
            return await _browser_agent_publish(text, "Cliquei, chefe." if ok else "Nao achei o que clicar, chefe.", turn_id=turn_id)
        # voltar PAGINA
        if re.search(r"\b(volta|voltar)\b", n) and not re.search(r"\b(segundo|um pouco|tras)\b", n) \
                and "video" not in n and "anterior" not in n:
            await _browser_control({"action": "back"})
            return await _browser_agent_publish(text, "Voltei pra pagina anterior.", turn_id=turn_id)
        # demais comandos via _parse_youtube_control -> teclado/scroll/Vision
        cmd = ""
        if _pyc is not None:
            fu = _pyc(n, str(text or ""))
            cmd = getattr(fu, "command", "") if getattr(fu, "matched", False) else ""
        _KEY = {"pause": "k", "play": "k", "play_pause": "k", "fullscreen": "f", "captions": "c",
                "mute": "m", "next": "Shift+N", "previous": "Shift+P", "from_start": "Home",
                "seek_forward": "l", "seek_back": "j"}
        _ACK = {"pause": "Pausei.", "play": "Voltando a tocar.", "next": "Proximo.", "previous": "Anterior.",
                "from_start": "Do comeco.", "fullscreen": "Tela cheia.", "captions": "Legenda.", "mute": "Mudo.",
                "seek_forward": "Avancei.", "seek_back": "Voltei um pouco."}
        if cmd in _KEY:
            await _browser_control({"action": "key", "key": _KEY[cmd]})
            return await _browser_agent_publish(text, _ACK.get(cmd, "Feito."), turn_id=turn_id)
        if cmd in ("scroll_down", "scroll_up"):
            amt = 650
            if re.search(r"\b(um pouco|pouquinho|pouco|levemente|devagar|so um)\b", n):
                amt = 450
            elif re.search(r"\b(bastante|bem mais|muito|tudo|ate o final|la em baixo|la embaixo)\b", n):
                amt = 1200
            await _browser_control({"action": "scroll", "direction": "down" if cmd == "scroll_down" else "up", "amount": amt})
            return await _browser_agent_publish(text, "Desci." if cmd == "scroll_down" else "Subi.", turn_id=turn_id)
        if cmd in ("like", "dislike", "subscribe", "unsubscribe", "notifications"):
            _instr = {"like": "clique no botao de curtir (joinha) do video",
                      "dislike": "clique no botao de nao curtir do video",
                      "subscribe": "clique no botao 'Inscrever-se' do canal",
                      "unsubscribe": "clique no botao 'Inscrito' para cancelar a inscricao",
                      "notifications": "clique no icone de sino de notificacoes"}[cmd]
            data = await _browser_control({"action": "vision_click", "instruction": _instr})
            ok = bool(data.get("ok"))
            return await _browser_agent_publish(text, "Feito, chefe." if ok else "Nao achei o botao, chefe.", turn_id=turn_id)
        return await _browser_agent_publish(text, "Nao peguei o comando, chefe. Pode repetir?", turn_id=turn_id)

    async def _chat_send_handle_browser(text: str, *, turn_id: str) -> dict | None:
        """FASE 1/4 (texto/HTTP) — MESMO fluxo por contexto do dispatcher de voz: detecta
        navegacao/controle, resolve alvo (embedded/local) e age via Browser Agent. Substitui
        os 3 handlers legados (que importavam modulos ja removidos na Fase 0)."""
        try:
            from agent.core.browser_agent_commands import (
                detect_browser_agent_command, is_navigation_command, is_control_command, resolve_open_target, build_open_url,
                youtube_query_of,
            )
        except Exception as exc:
            logger.warning("CHAT_BROWSER_GATE_UNAVAILABLE error=%s", exc)
            return None
        tasks = app.state.browser_tasks
        if tasks.status().get("state") == "waiting":
            # o navegador perguntou algo: o texto digitado é a resposta ("cancela" para a tarefa)
            if re.search(r"\b(cancela\w*|esquece|desiste|para a tarefa)\b", str(text or "").lower()):
                await tasks.stop()
                return await _browser_agent_publish(text, "Parei a tarefa no navegador.", turn_id=turn_id)
            answered = await tasks.answer(text)
            return await _browser_agent_publish(text, "Certo, continuando." if answered.get("ok") else "O navegador nao esta esperando resposta agora.", turn_id=turn_id)
        live = await app.state.live_browser.state()
        tab_open = bool(live.get("open")) and int(live.get("viewers") or 0) > 0
        is_nav = is_navigation_command(text)
        is_ctl = is_control_command(text)
        if not is_nav and not (tab_open and is_ctl):
            return None
        logger.info("CHAT_BROWSER_DISPATCH source=text tab_open=%s nav=%s ctl=%s",
                    str(tab_open).lower(), str(is_nav).lower(), str(is_ctl).lower())
        # CASO: aba ABERTA + controle (nao-navegacao)
        if tab_open and is_ctl and not is_nav:
            return await _chat_send_browser_control(text, turn_id=turn_id)
        # CASO: várias etapas (formulário, login, "vê o que aparece"): tarefa no navegador ao vivo
        command = detect_browser_agent_command(text)
        if command.action == "multistep":
            from services.live_browser import normalize_target

            await tasks.start(text, (normalize_target(command.url) or "") if command.url else "")
            try:
                _open_browser_tab_ui()
            except Exception:
                pass
            return await _browser_agent_publish(text, "Comecei no navegador, aqui na aba Internet. Se precisar de algo, eu te pergunto.", turn_id=turn_id)
        # CASO: NAVEGACAO
        target = resolve_open_target(text)
        logger.info("BROWSER_OPEN_TARGET target=%s", target)
        if target == "local":
            url = build_open_url(text) or "https://www.google.com"
            try:
                from agent.pc_control import open_in_local_browser
                await asyncio.to_thread(open_in_local_browser, url, text)
            except Exception as exc:
                logger.warning("CHAT_BROWSER_LOCAL_OPEN_FAILED error=%s", exc)
                return await _browser_agent_publish(text, "Nao consegui abrir no seu navegador, chefe.", turn_id=turn_id)
            return await _browser_agent_publish(text, "Pronto, chefe. Abri no seu navegador.", turn_id=turn_id)
        # embedded: o navegador da Kira, ao vivo na aba Internet
        yq = youtube_query_of(text)
        if yq:
            data = await _browser_control({"action": "youtube_open", "q": yq})
            if not data.get("ok"):
                return await _browser_agent_publish(text, "Nao consegui abrir o YouTube aqui agora, chefe.", turn_id=turn_id)
            via = str(data.get("via") or "")
            try:
                _open_browser_tab_ui()
            except Exception:
                pass
            msg = "Pronto, chefe. Ja coloquei aqui na aba." if via == "memory" else "Pronto, chefe. Procurei aqui na aba Internet."
            return await _browser_agent_publish(text, msg, turn_id=turn_id)
        url = build_open_url(text) or "https://www.google.com"
        logger.info("BROWSER_OPEN_EMBEDDED url=%s", url)
        try:
            await app.state.live_browser.navigate(url)
        except Exception as exc:
            logger.warning("CHAT_BROWSER_OPEN_FAILED reason=%s", type(exc).__name__)
            return await _browser_agent_publish(text, "Nao consegui abrir essa pagina aqui agora, chefe.", turn_id=turn_id)
        try:
            _open_browser_tab_ui()
        except Exception:
            pass
        return await _browser_agent_publish(text, "Pronto, chefe. Abri aqui na aba Internet.", turn_id=turn_id)

    async def _chat_send_handle_whatsapp_command(text: str, *, turn_id: str) -> dict | None:
        # ============================================================================
        # LEGADO DESATIVADO (unificacao no V9). O WhatsApp agora e tratado
        # EXCLUSIVAMENTE pelo agente: Gemini -> tool enviar_whatsapp ->
        # WhatsappOrchestrator (agent/core/whatsapp), tanto por VOZ quanto por TEXTO
        # digitado (o texto vai pro agente pelo data channel do LiveKit, nao por este
        # endpoint de host). Este atalho do host fazia um envio PARALELO text_only (sem
        # voz) e era a origem do WinError 267 / text_only / conflito reportado. Retorna
        # None pra NAO interceptar nada -> o comando de WhatsApp segue inteiro pro V9,
        # com todas as correcoes (Fases 1-4). O codigo abaixo fica inerte (nao executa).
        # ============================================================================
        logger.info("WHATSAPP_HOST_TEXT_ROUTE_DISABLED reason=unified_on_v9_agent")
        return None

    async def _chat_send_handle_memory_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.core.memory_commands import handle_memory_command
        except Exception as exc:
            logger.warning("MEMORY_COMMAND_RESOLVER_UNAVAILABLE error=%s", exc)
            return None

        try:
            result = await asyncio.to_thread(handle_memory_command, text, "chat")
        except Exception as exc:
            logger.warning("MEMORY_COMMAND_EXECUTE_FAILED_NON_FATAL error=%s", exc)
            return None
        if not bool(getattr(result, "handled", False)):
            return None

        await _chat_send_publish_user(text, turn_id=turn_id)
        logger.info("BROWSER_ROUTE_BLOCKED reason=memory_command")
        logger.info("GEMINI_ROUTE_BLOCKED reason=memory_command")
        logger.info("VOICE_ROUTE_BLOCKED reason=memory_command")
        notice = str(getattr(result, "reply", "") or "Fechado, chefe. Memoria atualizada.").strip()
        sensitive_revealed = bool(getattr(result, "metadata", {}) and getattr(result, "metadata", {}).get("revealed"))
        if sensitive_revealed:
            logger.info("SENSITIVE_VAULT_REVEAL_RESPONSE_SUPPRESSED_FROM_TRANSCRIPT")
        else:
            await _chat_send_publish_kira(
                notice,
                source="memory",
                turn_id=turn_id,
                transcript_kind="memory_command",
                phase="final",
            )
        try:
            from services.local_command_guard import mark_local_command
            mark_local_command("memory_command", seconds=5.0)
        except Exception:
            pass
        logger.info("MEMORY_COMMAND_HANDLED_TEXT_ONLY action=%s type=%s subject=%s", getattr(result, "action", ""), getattr(result, "memory_type", ""), getattr(result, "subject", ""))
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=memory_command turn_id=%s", turn_id)
        logger.info("TRANSCRIPTION_ORDER_ENFORCED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {
            "status": "ok",
            "reply": notice if sensitive_revealed else "",
            "handled": True,
            "reason": "memory_command",
            "voice": False,
            "gemini": False,
            "sensitive_revealed": sensitive_revealed,
            "timestamp": time.time(),
        }

    def _chat_send_explicit_browser_intent(text: str) -> bool:
        raw = str(text or "").strip().lower()
        normalized = unicodedata.normalize("NFD", raw)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        normalized = re.sub(r"[^\w\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return bool(re.search(
            r"\b(pesquis|procura|busca|googla|abre|abrir|abri|acessa|acesse|entra|entrar|vai|va|ir|navega|navegue|"
            r"google|twitter|x|mercado livre|mercadolivre|facebook|gmail|netflix|youtube|instagram|github|"
            r"navegador|internet|web|clique|clica|preencha|resuma a pagina|login|logar)\b",
            normalized,
        ))

    async def _chat_send_handle_memory_contextual_reply(text: str, *, turn_id: str) -> dict | None:
        if _chat_send_explicit_browser_intent(text):
            return None
        try:
            from services.memory_retriever import retrieve_context_for_text
        except Exception as exc:
            logger.warning("MEMORY_RETRIEVER_UNAVAILABLE error=%s", exc)
            return None
        try:
            memory_payload = await asyncio.to_thread(retrieve_context_for_text, text, source="chat", max_items=5)
        except Exception as exc:
            logger.warning("MEMORY_RETRIEVER_FAILED_NON_FATAL source=chat_pre_browser error=%s", exc)
            return None
        memory_context = str(memory_payload.get("text") or "").strip() if isinstance(memory_payload, dict) else ""
        if not memory_context:
            return None
        logger.info("MEMORY_CONTEXT_INJECTED source=chat_pre_browser")
        try:
            await _chat_send_publish_user(text, turn_id=turn_id)
        except Exception as exc:
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=text error=%s", exc)
        try:
            reply = await _handle_text_only_chat_message(text, memory_context=memory_context)
        except Exception as exc:
            logger.warning("TEXT_ONLY_CHAT_FAILED error=%s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        try:
            await transcription_bus.send_transcription(
                text=reply,
                speaker="kira",
                source="gemini",
                final=True,
                extra={"role": "assistant"},
            )
            logger.info("TRANSCRIPT_ASSISTANT_PUBLISHED source=gemini")
        except Exception as exc:
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=gemini error=%s", exc)
        return {
            "status": "ok",
            "reply": reply,
            "voice_mode": "text_only",
            "timestamp": time.time(),
        }

    async def _chat_send_handle_local_command(text: str, *, turn_id: str) -> dict | None:
        try:
            from agent.core.local_command_gate import detect_local_command
            from agent.core.local_command_executor import execute_local_command
            from agent.core.visual_reference_resolver import resolve_visual_local_command
        except Exception as exc:
            logger.warning("LOCAL_COMMAND_RESOLVER_UNAVAILABLE error=%s", exc)
            return None

        visual_resolution = resolve_visual_local_command(text)
        if visual_resolution.handled and visual_resolution.notice:
            await _chat_send_publish_user(text, turn_id=turn_id)
            logger.info("LOCAL_COMMAND_PLAN_CREATED kind=visual_reference action=clarify target=ambiguous")
            logger.info("GEMINI_NORMAL_REPLY_BLOCKED reason=local_command_mode")
            await _chat_send_publish_kira(
                visual_resolution.notice,
                source=visual_resolution.source_label,
                turn_id=turn_id,
                transcript_kind="local_command",
                phase="final",
            )
            logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
            logger.info("GEMINI_ROUTE_BLOCKED reason=%s", visual_resolution.source_label)
            logger.info("VOICE_ROUTE_BLOCKED reason=%s", visual_resolution.source_label)
            logger.info("LOCAL_COMMAND_TURN_HANDLED source=%s turn_id=%s", visual_resolution.source_label, turn_id)
            logger.info("TRANSCRIPTION_ORDER_ENFORCED")
            logger.info("LOCAL_COMMAND_TURN_CLOSED")
            return {"status": "ok", "reply": "", "handled": True, "reason": visual_resolution.source_label, "voice": False, "gemini": False, "timestamp": time.time()}

        cmd = visual_resolution.intent if visual_resolution.intent is not None else detect_local_command(text)
        if cmd is None:
            logger.info("WINDOW_CONTEXT_NOT_POLLED reason=not_needed")
            return None

        source_label = "window_command" if cmd.kind == "window" else ("file_app_command" if cmd.kind in {"file", "app"} else "local_command")
        if source_label == "window_command":
            logger.info("WINDOW_COMMAND_DETECTED action=%s target=%s", cmd.action, cmd.target)
        elif source_label == "file_app_command":
            logger.info("FILE_APP_COMMAND_DETECTED action=%s target=%s", cmd.action, cmd.target)

        await _chat_send_publish_user(text, turn_id=turn_id)
        logger.info(
            "LOCAL_COMMAND_TRANSCRIPT_ORDER_BEGIN kind=%s action=%s target=%s",
            cmd.kind,
            cmd.action,
            cmd.target,
        )
        logger.info(
            "LOCAL_COMMAND_PLAN_CREATED kind=%s action=%s target=%s",
            cmd.kind,
            cmd.action,
            cmd.target,
        )
        logger.info(
            "GEMINI_COMMAND_MODE_ENTER kind=%s action=%s target=%s",
            cmd.kind,
            cmd.action,
            cmd.target,
        )
        logger.info("GEMINI_NORMAL_REPLY_BLOCKED reason=local_command_mode")
        await _chat_send_publish_kira(
            cmd.ack_before(),
            source=source_label,
            turn_id=turn_id,
            transcript_kind="local_command",
            phase="pre",
        )
        logger.info("LOCAL_COMMAND_PRE_ACK_PUBLISHED")
        logger.info(
            "LOCAL_COMMAND_BACKEND_EXECUTE_BEGIN kind=%s action=%s target=%s",
            cmd.kind,
            cmd.action,
            cmd.target,
        )
        try:
            result = await asyncio.to_thread(execute_local_command, cmd)
        except Exception as exc:
            logger.exception("LOCAL_COMMAND_BACKEND_EXECUTE_FAILED kind=%s action=%s error=%s", cmd.kind, cmd.action, exc)
            result_message = "Interface: nao consegui executar esse comando local."
        else:
            logger.info("LOCAL_COMMAND_BACKEND_EXECUTE_OK kind=%s action=%s", cmd.kind, cmd.action)
            result_message = result.message
        logger.info("LOCAL_COMMAND_RESULT_SENT_TO_ASSISTANT")
        logger.info("GEMINI_COMMAND_RESULT_CONTEXT_SENT")
        await _chat_send_publish_kira(
            result_message,
            source=source_label,
            turn_id=turn_id,
            transcript_kind="local_command",
            phase="final",
        )
        logger.info("LOCAL_COMMAND_FINAL_ACK_PUBLISHED")
        logger.info("LOCAL_COMMAND_FINAL_ACK_ORDERED_AFTER_RESULT")
        logger.info("GEMINI_ROUTE_BLOCKED reason=%s", source_label)
        logger.info("VOICE_ROUTE_BLOCKED reason=%s", source_label)
        logger.info("LOCAL_COMMAND_TURN_HANDLED source=%s turn_id=%s", source_label, turn_id)
        logger.info("TRANSCRIPTION_ORDER_ENFORCED")
        logger.info("LOCAL_COMMAND_TURN_CLOSED")
        return {"status": "ok", "reply": "", "handled": True, "reason": source_label, "voice": False, "gemini": False, "timestamp": time.time()}

    @app.post("/api/session-recap/user")
    async def session_recap_user(payload: dict = Body(default=None)):
        """Texto digitado com a sessão aberta vai pelo canal de dados direto ao agente; a interface avisa aqui."""
        text = str((payload or {}).get("text") or "").strip()[:2000] if isinstance(payload, dict) else ""
        if not text:
            raise HTTPException(status_code=422, detail="text ausente")
        get_session_recap().add("user", text)
        note_user_text(text)
        return {"ok": True}

    @app.post("/api/chat/send")
    async def chat_send(request: Request):
        logger.info("API_CHAT_SEND_BEGIN")
        # Parse tolerante de encoding: alguns clientes mandam acentos em cp1252/
        # latin-1 (nao-UTF-8). Em vez de devolver 400 "error parsing body", a Kira
        # decodifica com fallback. Evita HTTP 400 em frases acentuadas
        # ("a senha e...", "cade o ultimo arquivo?").
        payload: dict = {}
        try:
            raw = await request.body()
        except Exception:
            raw = b""
        if raw:
            decoded = None
            for _enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
                try:
                    decoded = raw.decode(_enc)
                    break
                except Exception:
                    continue
            if decoded is not None:
                try:
                    parsed = json.loads(decoded)
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:
                    logger.info("API_CHAT_SEND_BODY_NON_JSON treated_as=empty")
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Mensagem vazia.")

        with _runtime_lock:
            state = _runtime_readiness.as_dict()

        accepted, mode, reason, voice_ready = _chat_send_text_mode(state)
        _chat_send_log_session_status(state, text_ready=accepted, mode=mode, reason=reason, voice_ready=voice_ready)

        # Diagnostico explicito da decisao de rota: por design /api/chat/send e o
        # canal de TEXTO. A voz fisica e produzida pelo pipeline de microfone do
        # LiveKit, nunca por este endpoint. Os campos abaixo deixam claro o porque
        # da resposta sair como text_only (em vez de "sem motivo").
        functional_ready = bool(state.get("functional_ready"))
        runtime_ready = bool(state.get("runtime_ready"))
        gemini_ready = bool(state.get("gemini_ready"))
        voice_pipeline_ready = bool(state.get("voice_pipeline_ready"))
        room_connected = bool(state.get("room_connected"))
        voice_usable = bool(functional_ready and voice_pipeline_ready and room_connected)
        if not accepted:
            reason_for_text_only = f"text_mode_blocked:{reason}"
        elif voice_usable:
            # Voz utilizavel, mas o canal de texto digitado nunca fala por design.
            reason_for_text_only = "text_channel_by_design"
        else:
            reason_for_text_only = f"voice_not_usable:{reason}"
        logger.info(
            "API_CHAT_SEND_ROUTE_DECIDED chat_send_requested=true runtime_ready=%s functional_ready=%s "
            "gemini_ready=%s voice_pipeline_ready=%s text_mode_active=%s voice_usable=%s mode=%s reason=%s",
            str(runtime_ready).lower(),
            str(functional_ready).lower(),
            str(gemini_ready).lower(),
            str(voice_pipeline_ready).lower(),
            str(bool(accepted)).lower(),
            str(voice_usable).lower(),
            mode,
            reason,
        )
        logger.info("API_CHAT_SEND_VOICE_DECISION voice_usable=%s reason_for_text_only=%s", str(voice_usable).lower(), reason_for_text_only)
        if not accepted:
            logger.info("API_CHAT_SEND_REJECTED reason=%s", reason)
            raise HTTPException(status_code=409, detail="Modo texto nao esta ativo no momento.")

        _log_accepted_text_input(text)
        # Texto digitado não passa pelo barramento de transcrição: entra aqui no registro da conversa e nos interesses.
        get_session_recap().add("user", text)
        note_user_text(text)
        logger.info("AGENT_TEXT_RECEIVED_FROM_UI text=%r", _safe_user_text_preview(text))
        try:
            from agent.volume_intent_resolver import is_volume_command_candidate
            if is_volume_command_candidate(text, has_recent_context=bool(_chat_send_volume_context.get("action"))):
                logger.info("SHORT_COMMAND_DELIVERED_TO_AGENT")
        except Exception:
            pass
        logger.info("API_CHAT_SEND_ACCEPTED mode=%s", mode)
        logger.info("TEXT_FALLBACK_DOES_NOT_RESTART_VOICE")
        logger.info("VOICE_PIPELINE_NOT_RESTARTED reason=text_send")
        turn_id = str(payload.get("turn_id") or f"chat-send-{int(time.time() * 1000)}")

        economy_manager = get_economy_mode_manager()
        economy_status_snapshot = economy_manager.get_status(runtime_snapshot=state)
        if bool(economy_status_snapshot.get("economy_standby")):
            executor_result = None
            wake_word = ""
            try:
                from services.wake_word_runtime import extract_wake_query
                wake_query_snapshot = extract_wake_query(text, economy_manager.get_config().get("wake_words", []))
                wake_word = str(wake_query_snapshot.get("wake_word") or "")
            except Exception:
                wake_word = ""
            if wake_word and economy_manager.real_gemini_standby_allowed():
                executor_result = await _call_agent_gemini_economy_control("economy/gemini-wake")
            standby_text = economy_manager.handle_standby_text(
                text,
                executor=(lambda: executor_result) if executor_result is not None else None,
                source="chat_send",
            )
            if standby_text.get("route") == "ignored":
                logger.info("API_CHAT_SEND_IGNORED reason=economy_standby_no_wake")
                return {
                    "status": "ok",
                    "reply": "",
                    "handled": True,
                    "reason": standby_text.get("reason") or "economy_standby",
                    "voice": False,
                    "gemini": False,
                    "economy_standby": True,
                    "timestamp": time.time(),
                }
            if standby_text.get("route") == "wake":
                if not bool(standby_text.get("ok")):
                    detail = standby_text.get("reason") or standby_text.get("error") or "unknown"
                    logger.warning("API_CHAT_SEND_WAKE_FAILED reason=%s", detail)
                    return {
                        "status": "error",
                        "reply": "",
                        "handled": True,
                        "reason": "gemini_wake_failed",
                        "detail": detail,
                        "error": detail,
                        "voice": False,
                        "gemini": False,
                        "economy_standby": True,
                        "state": standby_text.get("state"),
                        "timestamp": time.time(),
                    }
                remaining_query = str(standby_text.get("remaining_user_query") or "").strip()
                if remaining_query:
                    from services.economy_wake_query import build_wake_query_dispatch_response

                    logger.info("ECONOMY_WAKE_QUERY_DISPATCH_BEGIN target=client_data_channel source=chat_send")
                    economy_manager.reset_idle_timer(reason="chat_send_wake_query")
                    dispatch_response = build_wake_query_dispatch_response(standby_text)
                    if dispatch_response is not None:
                        logger.info("ECONOMY_WAKE_QUERY_DISPATCH_OK target=client_data_channel_pending source=chat_send")
                        return dispatch_response
                    logger.warning("ECONOMY_WAKE_QUERY_DISPATCH_FAILED target=client_data_channel reason=payload_unavailable")
                response_text = str(standby_text.get("response") or "To aqui, chefe.").strip()
                try:
                    await _chat_send_publish_user(text, turn_id=turn_id)
                    await _chat_send_publish_kira(response_text, source="economy_wake", turn_id=turn_id, phase="final")
                except Exception as exc:
                    logger.warning("ECONOMY_WAKE_TEXT_TRANSCRIPT_FAILED_NON_FATAL error=%s", exc)
                return {
                    "status": "ok",
                    "reply": response_text,
                    "handled": True,
                    "reason": "economy_wake",
                    "voice": False,
                    "gemini": False,
                    "economy_standby": False,
                    "timestamp": time.time(),
                }

        economy_manager.reset_idle_timer(reason="chat_send_active_text")

        if _chat_send_is_incomplete_memory_reference(text):
            try:
                await _chat_send_publish_user(text, turn_id=turn_id)
            except Exception as exc:
                logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=text error=%s", exc)
            _chat_send_reject_incomplete_memory_reference(turn_id=turn_id)
            return {
                "status": "ok",
                "reply": "",
                "handled": True,
                "reason": "incomplete_memory_reference",
                "voice": False,
                "gemini": False,
                "timestamp": time.time(),
            }

        try:
            from agent.core.safety_gate import check_safety_gate
        except Exception:
            check_safety_gate = None
        if callable(check_safety_gate):
            safety_result = check_safety_gate(text)
            if bool(getattr(safety_result, "matched", False)):
                try:
                    await _chat_send_publish_user(text, turn_id=turn_id)
                except Exception as exc:
                    logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=text error=%s", exc)
                response_text = str(getattr(safety_result, "response", "") or "").strip()
                if response_text:
                    await _chat_send_publish_kira(
                        response_text,
                        source="safety_gate",
                        turn_id=turn_id,
                        transcript_kind="safety_gate",
                        phase="final",
                    )
                if str(getattr(safety_result, "reason", "") or "") == "adult_safe_response":
                    logger.info("ADULT_SAFE_RESPONSE_ROUTE reason=clear_adult_non_explicit source=chat_send")
                    logger.info(
                        "ADULT_SAFE_RESPONSE_DELIVERED turn_id=%s source=chat_send text_preview=%r delivered=true voice=false",
                        turn_id,
                        _safe_user_text_preview(response_text, max_chars=80),
                    )
                logger.info("GEMINI_ROUTE_BLOCKED reason=safety_gate")
                logger.info("VOICE_ROUTE_BLOCKED reason=safety_gate")
                return {
                    "status": "ok",
                    "reply": response_text,
                    "handled": True,
                    "reason": "safety_gate",
                    "voice": False,
                    "gemini": False,
                    "voice_mode": "text_only",
                    "timestamp": time.time(),
                }

        try:
            # HOTFIX 3.3: follow-up de job ativo do Browser Agent tem PRIORIDADE
            # (logo apos safety), antes de panel/volume/local/artifact/creation/chat.
            # FASE 1/4 — UM único caminho de browser por texto (mesmo dispatcher de voz).
            # Substitui os 3 handlers legados (followup/agent_command/command) que importavam
            # módulos removidos na Fase 0 e abriam caminhos paralelos conflitantes.
            browser_result = await _chat_send_handle_browser(text, turn_id=turn_id)
            if browser_result is not None:
                return browser_result
            panel_result = await _chat_send_handle_panel_command(text, turn_id=turn_id)
            if panel_result is not None:
                return panel_result
            volume_result = await _chat_send_handle_volume_command(text, turn_id=turn_id)
            if volume_result is not None:
                return volume_result
            local_file_result = await _chat_send_handle_local_file_command(text, turn_id=turn_id)
            if local_file_result is not None:
                return local_file_result
            artifact_result = await _chat_send_handle_artifact_command(text, turn_id=turn_id)
            if artifact_result is not None:
                return artifact_result
            creation_result = await _chat_send_handle_creation_task_command(text, turn_id=turn_id)
            if creation_result is not None:
                return creation_result
            memory_result = await _chat_send_handle_memory_command(text, turn_id=turn_id)
            if memory_result is not None:
                return memory_result
            whatsapp_result = await _chat_send_handle_whatsapp_command(text, turn_id=turn_id)
            if whatsapp_result is not None:
                return whatsapp_result
            memory_contextual_result = await _chat_send_handle_memory_contextual_reply(text, turn_id=turn_id)
            if memory_contextual_result is not None:
                return memory_contextual_result
            local_result = await _chat_send_handle_local_command(text, turn_id=turn_id)
            if local_result is not None:
                return local_result
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("LOCAL_COMMAND_TEXT_FALLBACK_FAILED_NON_FATAL error=%s", exc)

        try:
            await _chat_send_publish_user(text, turn_id=turn_id)
        except Exception as exc:
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=text error=%s", exc)
        memory_context = ""
        try:
            from services.memory_retriever import retrieve_context_for_text
            memory_payload = await asyncio.to_thread(retrieve_context_for_text, text, source="chat", max_items=5)
            memory_context = str(memory_payload.get("text") or "").strip() if isinstance(memory_payload, dict) else ""
            if memory_context:
                logger.info("MEMORY_CONTEXT_INJECTED source=chat")
        except Exception as exc:
            logger.warning("MEMORY_RETRIEVER_FAILED_NON_FATAL source=chat error=%s", exc)
        try:
            reply = await _handle_text_only_chat_message(text, memory_context=memory_context)
        except asyncio.TimeoutError:
            # Timeout na geracao do Gemini de texto. Estado pendente ja foi limpo
            # dentro de _handle_text_only_chat_message; o turno seguinte fica livre.
            logger.warning("API_CHAT_SEND_TIMEOUT_STAGE stage=gemini_text seconds=%s", TEXT_ONLY_CHAT_TIMEOUT_SECONDS)
            logger.info("API_CHAT_SEND_STATE_CLEANUP reason=gemini_timeout turn_id=%s", turn_id)
            fallback_reply = "Demorei demais pra responder agora, chefe. Tenta de novo num instante."
            try:
                await transcription_bus.send_transcription(
                    text=fallback_reply,
                    speaker="kira",
                    source="gemini",
                    final=True,
                    extra={"role": "assistant"},
                )
            except Exception as pub_exc:
                logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=gemini_timeout error=%s", pub_exc)
            logger.info("API_CHAT_SEND_DONE status=timeout voice_mode=text_only reason=gemini_timeout")
            return {
                "status": "ok",
                "reply": fallback_reply,
                "voice_mode": "text_only",
                "reason": "gemini_timeout",
                "timestamp": time.time(),
            }
        except Exception as exc:
            logger.warning("TEXT_ONLY_CHAT_FAILED error=%s", exc)
            logger.info("API_CHAT_SEND_STATE_CLEANUP reason=text_only_failed turn_id=%s", turn_id)
            logger.info("API_CHAT_SEND_DONE status=error voice_mode=text_only reason=text_only_failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            await transcription_bus.send_transcription(
                text=reply,
                speaker="kira",
                source="gemini",
                final=True,
                extra={"role": "assistant"},
            )
            logger.info("TRANSCRIPT_ASSISTANT_PUBLISHED source=gemini")
        except Exception as exc:
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_NON_FATAL source=gemini error=%s", exc)
        logger.info("API_CHAT_SEND_DONE status=ok voice_mode=text_only reason=%s", reason_for_text_only)
        return {
            "status": "ok",
            "reply": reply,
            "voice_mode": "text_only",
            "reason": reason_for_text_only,
            "timestamp": time.time(),
        }

    @app.get("/api/voice/status")
    async def voice_status():
        logger.info("VOICE_STATUS_ENDPOINT_REQUEST")
        try:
            state = _runtime_state_snapshot()
            telemetry = _agent_telemetry_snapshot()
            voice_snap = telemetry.get("voice_runtime_snapshot") or {}
            functional_ready = bool(state.get("functional_ready"))
            voice_pipeline_ready = bool(state.get("voice_pipeline_ready"))
            room_connected = bool(state.get("room_connected") or state.get("room_connecting"))
            session_failed = bool(state.get("session_failed"))
            _htel = telemetry.get("health") or {}
            _part_linked = bool(telemetry.get("participant_linked") or _htel.get("participant_linked"))
            _mic_sub = bool(telemetry.get("mic_subscribed") or _htel.get("mic_subscribed"))
            _voice_usable = bool(functional_ready and _part_linked and _mic_sub)
            if not room_connected:
                voice_st = "standby"
            elif session_failed:
                voice_st = "error"
            elif _voice_usable:
                voice_st = "online"
            elif functional_ready and not _mic_sub:
                voice_st = "waiting_microphone"
            elif functional_ready:
                voice_st = "online"
            elif voice_pipeline_ready:
                voice_st = "starting"
            else:
                voice_st = "starting"
            result = {
                "ok": True,
                "source": "agent_snapshot" if voice_snap else "backend_runtime_state",
                "voice": {
                    "status": voice_st,
                    "state": voice_snap.get("state", state.get("agent_state", "unknown")),
                    "queue_size": voice_snap.get("queue_size"),
                    "current_speech_id": voice_snap.get("current_speech_id"),
                    "current_source": voice_snap.get("current_source"),
                    "current_text_preview": voice_snap.get("current_text_preview"),
                    "state_age_ms": voice_snap.get("state_age_ms"),
                    "last_audio_chunk_age_ms": voice_snap.get("last_audio_chunk_age_ms"),
                    "speaking_since_ms": voice_snap.get("speaking_since_ms"),
                    "watchdog_recovery_count": voice_snap.get("watchdog_recovery_count"),
                    "tool_ack_active": voice_snap.get("tool_ack_active", False),
                    "tool_ack_active_until_ms": voice_snap.get("tool_ack_active_until_ms"),
                    "functional_ready": functional_ready,
                    "room_connected": room_connected,
                },
                "timestamp": time.time(),
            }
            logger.info("VOICE_STATUS_ENDPOINT_RESPONSE ok=true source=%s", result["source"])
            return result
        except Exception as exc:
            logger.warning("VOICE_STATUS_ENDPOINT_FALLBACK reason=%s", exc)
            return {
                "ok": False,
                "source": "fallback",
                "voice": {"status": "unknown"},
                "timestamp": time.time(),
                "error": str(exc),
            }

    @app.get("/api/features")
    async def api_features():
        modules = get_optional_modules()
        return {
            "ok": True,
            "core_mode": bool(load_kira_features().get("core_mode", True)),
            "core": {
                "backend": True,
                "livekit": True,
                "agent": True,
                "gemini": True,
                "voice": True,
                "speech_queue": True,
                "turns": True,
                "final_only_transcription": True,
                "functional_ready": True,
            },
            "optional_modules": modules,
            "timestamp": time.time(),
        }

    @app.get("/api/features/runtime-status")
    async def api_features_runtime_status():
        from services.feature_runtime_registry import feature_runtime_status

        return {"success": True, "data": feature_runtime_status(), "error": None}

    @app.get("/api/system-status")
    async def system_status():
        try:
            return await _status_response_cache.get_or_compute_async(
                "GET /api/system-status",
                2.0,
                _system_status_inner,
            )
        except Exception as _ss_exc:
            logger.error("SYSTEM_STATUS_SAFE_FALLBACK error=%s", _ss_exc)
            return {
                "ok": False,
                "brain": {"status": "offline", "label": "Offline", "color": "red"},
                "livekit": {"status": "offline", "label": "Offline", "color": "red"},
                "voice": {"status": "standby", "label": "Pronta", "color": "green"},
                "api": {"status": "warning", "label": "API ?/?", "color": "yellow"},
                "whatsapp": {"status": "offline", "label": "Offline", "color": "red"},
                "health": {"level": "attention", "label": "Atenção", "color": "yellow"},
                "timestamp": time.time(),
                "error": str(_ss_exc),
            }

    @app.get("/api/api-identity/snapshot")
    async def api_identity_snapshot():
        return get_api_identity_snapshot(provider=LIVEKIT_SELECTED_PROVIDER)

    async def _system_status_inner():
        state = _runtime_state_snapshot()
        canonical_runtime = _sync_runtime_status_manager_from_legacy(state, already_augmented=True)
        # Os dois endpoints continuam com formatos diferentes por
        # compatibilidade, mas os sinais de prontidão vêm da mesma fotografia.
        for key in (
            "backend_ready", "livekit_ready", "worker_ready", "room_ready",
            "gemini_ready", "voice_pipeline_ready", "runtime_ready", "functional_ready",
        ):
            if key in canonical_runtime:
                state[key] = canonical_runtime[key]
        state["core_ready"] = bool(canonical_runtime.get("core_ready"))
        state["runtime_phase"] = canonical_runtime.get("phase")
        telemetry = _agent_telemetry_snapshot()
        key_report = telemetry.get("keys") or _load_agent_key_report()
        api_identity = get_api_identity_snapshot(
            provider=LIVEKIT_SELECTED_PROVIDER,
            key_report=key_report if isinstance(key_report, dict) else None,
        )
        _rate_limited_log("system_status_endpoint_request", "SYSTEM_STATUS_ENDPOINT_REQUEST", interval_s=30.0)
        total_keys = int(key_report.get("usable_keys", 0) or key_report.get("configured_keys", 0) or 0)
        current_text_index = key_report.get("current_text_index")
        api_total = max(total_keys, 4)
        api_current = (int(current_text_index) + 1) if current_text_index is not None else None

        brain_status = "error" if state.get("session_failed") else "offline"
        if state.get("functional_ready"):
            brain_status = "online"
        elif state.get("boot_recovery_active") or state.get("room_connecting") or state.get("agent_process_spawned") or state.get("backend_ready"):
            brain_status = "recovering"

        lk_raw = str(state.get("livekit_status") or "").strip().lower()
        if lk_raw in {"ready", "ok", "connected"}:
            livekit_status = "connected"
        elif lk_raw in {"starting", "connecting", "unknown"} or state.get("room_connecting"):
            livekit_status = "connecting"
        elif lk_raw in {"auth_failed", "quota_exceeded", "all_failed", "error"}:
            livekit_status = "error"
        else:
            livekit_status = "offline"

        session_active = bool(
            state.get("room_connected")
            or state.get("room_connecting")
            or state.get("session_started")
        )
        voice_pipeline_ready = bool(state.get("voice_pipeline_ready"))
        runtime_ready = bool(state.get("runtime_ready"))
        functional_ready = bool(state.get("functional_ready"))
        recovery_active = bool(state.get("boot_recovery_active"))
        # Participant/mic state from agent telemetry
        _health_tel = telemetry.get("health") or {}
        participant_linked = bool(
            telemetry.get("participant_linked")
            or _health_tel.get("participant_linked")
        )
        mic_subscribed = bool(
            telemetry.get("mic_subscribed")
            or _health_tel.get("mic_subscribed")
        )
        # voice_usable_ready: pipeline exists + mic linked
        voice_usable_ready = bool(functional_ready and participant_linked and mic_subscribed)
        if not session_active:
            voice_status = "standby"
            voice_label = "Standby"
        elif state.get("session_failed"):
            voice_status = "error"
            voice_label = "Erro"
        elif voice_usable_ready:
            voice_status = "online"
            voice_label = "Online"
        elif functional_ready and not mic_subscribed:
            voice_status = "waiting_microphone"
            voice_label = "Aguardando microfone"
        elif functional_ready:
            voice_status = "online"
            voice_label = "Online"
        elif recovery_active:
            voice_status = "recovering"
            voice_label = "Recuperando"
        else:
            voice_status = "starting"
            voice_label = "Inicializando"
        if state.get("session_failed"):
            voice_status = "error"
            voice_label = "Erro"
        reason = (
            "no_session" if not session_active
            else "voice_usable_ready" if voice_usable_ready
            else "mic_not_subscribed" if functional_ready and not mic_subscribed
            else "pipeline_and_runtime_ready" if voice_pipeline_ready and runtime_ready
            else "functional_ready" if functional_ready
            else "recovery_active" if recovery_active
            else "pipeline_not_ready"
        )
        _rate_limited_log(
            "voice_ready_gate_check",
            "VOICE_READY_GATE_CHECK pipeline=%s participant_linked=%s mic_subscribed=%s voice_usable=%s",
            voice_pipeline_ready, participant_linked, mic_subscribed, voice_usable_ready,
            interval_s=30.0,
        )
        _rate_limited_log(
            f"system_status_voice_state:{voice_status}:{reason}",
            "SYSTEM_STATUS_VOICE_STATE session_active=%s voice_pipeline_ready=%s runtime_ready=%s functional_ready=%s recovery_active=%s voice_status=%s reason=%s",
            session_active,
            voice_pipeline_ready,
            runtime_ready,
            functional_ready,
            recovery_active,
            voice_status,
            reason,
            interval_s=30.0,
        )
        prev_voice_status = getattr(_system_status_inner, "_prev_voice_status", None)
        if prev_voice_status != voice_status:
            logger.info(
                "VOICE_STATUS_TRANSITION old=%s new=%s reason=%s",
                prev_voice_status or "none",
                voice_status,
                reason,
            )
            _system_status_inner._prev_voice_status = voice_status

        if api_current is None:
            api_label = "API ?/?"
            api_color = "yellow"
            api_status = "warning"
        else:
            api_label = f"API {api_current}/{api_total}"
            if api_current >= 4:
                api_color, api_status = "red", "critical"
            elif api_current == 3:
                api_color, api_status = "yellow", "warning"
            else:
                api_color, api_status = "green", "online"
        _rate_limited_log(
            f"system_status_api_key:{api_current}:{api_total}:{api_color}",
            "SYSTEM_STATUS_API_KEY_STATE current=%s total=%s color=%s",
            api_current,
            api_total,
            api_color,
            interval_s=30.0,
        )

        try:
            if KIRA_BOOT_MINIMAL and not is_whatsapp_enabled():
                wa_status, wa_label, wa_color = "disabled", "Lazy", "muted"
                raise StopIteration
            wa_runtime = get_whatsapp_runtime_state()
            wa_state = str(wa_runtime.get("state") or "").strip().lower()
            wa_connected = bool(wa_runtime.get("connected"))
            wa_send_ready = bool(wa_runtime.get("send_ready"))
            wa_search_ready = bool(wa_runtime.get("search_ready"))
            wa_browser_alive = bool(wa_runtime.get("browser_alive"))
            wa_page_alive = bool(wa_runtime.get("page_alive"))
            wa_recovering = bool(wa_runtime.get("recovering"))
            if wa_connected and wa_send_ready and wa_search_ready and wa_browser_alive and wa_page_alive:
                wa_status, wa_label, wa_color = "connected", "Conectado", "green"
            elif wa_recovering or wa_state in {"booting", "bridge_starting", "reconnecting", "unstable", "connecting"}:
                wa_status, wa_label, wa_color = "reconnecting", "Reconectando", "yellow"
            elif wa_connected:
                wa_status, wa_label, wa_color = "reconnecting", "Não pronto", "yellow"
            elif wa_state in {"error"}:
                wa_status, wa_label, wa_color = "error", "Erro", "red"
            else:
                wa_status, wa_label, wa_color = "offline", "Offline", "red"
        except StopIteration:
            pass
        except Exception as exc:
            logger.warning("SYSTEM_STATUS_WHATSAPP_CHECK_FAILED error=%s", exc)
            wa_status, wa_label, wa_color = "error", "Erro", "red"
        if not is_optional_enabled("whatsapp"):
            wa_status, wa_label, wa_color = "disabled", "Desativado", "muted"

        cerebro_ok = brain_status == "online"
        livekit_ok = livekit_status == "connected"
        api_ok = api_current is not None and api_total > 0
        voice_ok = (voice_pipeline_ready and runtime_ready) if session_active else True
        readiness_can_start = bool(canonical_runtime.get("can_start_session"))
        failed_systems = sum([not cerebro_ok, not livekit_ok, not api_ok, not voice_ok, not readiness_can_start])
        if failed_systems == 0:
            health_level, health_label, health_color = "excellent", "Excelente", "green"
        elif failed_systems == 1:
            health_level, health_label, health_color = "good", "Boa", "green"
        elif failed_systems == 2:
            health_level, health_label, health_color = "attention", "Atenção", "yellow"
        else:
            health_level, health_label, health_color = "critical", "Crítica", "red"
        _rate_limited_log(
            f"system_status_health:{health_level}",
            "SYSTEM_STATUS_HEALTH level=%s",
            health_level,
            interval_s=30.0,
        )
        runtime_gemini_voice = _runtime_gemini_voice_status(state, telemetry)
        gemini_payload = dict(runtime_gemini_voice.get("gemini") or {})
        gemini_payload.update({"key_slot": api_current, "total_slots": api_total})
        voice_payload = {
            "status": voice_status,
            "label": voice_label,
            "color": "green" if voice_status in {"online", "standby"} else ("yellow" if voice_status in {"recovering", "starting", "waiting_microphone"} else "red"),
            **dict(runtime_gemini_voice.get("voice") or {}),
        }
        livekit_provider_kind = str(getattr(LIVEKIT_SELECTED_PROVIDER, "kind", "") or "").strip().lower()
        livekit_connected_label = "Conectado (Local)" if livekit_provider_kind == "local" else "Conectado (Cloud)"

        payload = {
            "ok": True,
            "brain": {"status": brain_status, "label": {"online": "Online", "recovering": "Recuperando", "offline": "Offline", "error": "Erro"}.get(brain_status, "Offline"), "color": "green" if brain_status == "online" else ("yellow" if brain_status == "recovering" else "red")},
            "livekit": {"status": livekit_status, "label": {"connected": livekit_connected_label, "connecting": "Conectando", "offline": "Offline", "error": "Erro"}.get(livekit_status, "Offline"), "color": "green" if livekit_status == "connected" else ("yellow" if livekit_status == "connecting" else "red")},
            "voice": voice_payload,
            "gemini": gemini_payload,
            "api": {"status": api_status, "label": api_label, "current": api_current, "total": api_total, "color": api_color},
            "api_identity": api_identity,
            "whatsapp": {"status": wa_status, "label": wa_label, "color": wa_color},
            "health": {"level": health_level, "label": health_label, "color": health_color},
            "timestamp": time.time(),
            "core_ready": bool(canonical_runtime.get("core_ready")),
            # Readiness canonica tambem fica no snapshot leve do painel. A UI
            # pode sair da tela de espera mesmo se um POST de evento perder a
            # janela de conexao durante o bootstrap.
            "room_connected": bool(state.get("room_connected")),
            "session_started": bool(state.get("session_started")),
            "gemini_ready": bool(state.get("gemini_ready")),
            "voice_pipeline_ready": voice_pipeline_ready,
            "runtime_ready": runtime_ready,
            "functional_ready": functional_ready,
            "ui_session_ready": functional_ready,
            "ui_status": "session_ready" if functional_ready else "booting",
            "phase": "functional_ready" if functional_ready else str(canonical_runtime.get("phase") or state.get("boot_state") or "booting").lower(),
            "backend_ready": bool(canonical_runtime.get("backend_ready")),
            "livekit_ready": bool(canonical_runtime.get("livekit_ready")),
            "worker_ready": bool(canonical_runtime.get("worker_ready")),
            "can_start_session": bool(canonical_runtime.get("can_start_session")),
            "optional_status": {
                name: optional_status_payload(name)
                for name in get_optional_modules()
            },
            "optional_degraded_services": {
                name: item
                for name, item in get_degraded_services().items()
                if is_optional_enabled(name)
            },
            # compatibilidade
            "overall": health_level,
            "livekit_warning": {"visible": bool(state.get("restart_required_for_voice")), "text": str(state.get("livekit_message") or "").strip()},
        }
        _rate_limited_log(
            f"system_status_computed:{brain_status}:{livekit_status}:{voice_status}:{api_label}:{wa_label}:{health_label}",
            "SYSTEM_STATUS_COMPUTED brain=%s livekit=%s voice=%s api=%s whatsapp=%s health=%s",
            brain_status, livekit_status, voice_status, api_label, wa_label, health_label,
            interval_s=30.0,
        )
        return payload

    @app.get("/api/artifacts/jobs")
    async def api_artifact_jobs(limit: int = 20):
        logger.info("ARTIFACT_API_JOBS_REQUEST")
        return {"ok": True, "jobs": list_recent_jobs(limit=limit)}

    @app.get("/api/artifacts/jobs/{job_id}")
    async def api_artifact_job_detail(job_id: str):
        logger.info("ARTIFACT_API_JOB_DETAIL_REQUEST job_id=%s", job_id)
        job = get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return {"ok": True, "job": job}

    @app.get("/api/artifacts/recent")
    async def api_artifact_recent(limit: int = 20, type: str | None = None):
        logger.info("ARTIFACT_API_RECENT_REQUEST")
        return {"ok": True, "artifacts": list_recent_artifacts(limit=limit, kind=type)}

    @app.get("/api/artifacts/last")
    async def api_artifact_last(type: str | None = None):
        return {"ok": True, "artifact": get_last_artifact(kind=type)}

    @app.get("/api/artifacts/status")
    async def api_artifact_status():
        # Reconcilia jobs/artifacts presos antes de reportar status, sem apagar
        # historico. Jobs antigos em "running" viram stale_failed/orphaned;
        # pendentes invalidos viram missing_file/expired_waiting_destination.
        reconcile_summary = {}
        try:
            reconcile_summary = await asyncio.to_thread(reconcile_jobs)
        except Exception as exc:
            logger.warning("ARTIFACT_STATUS_RECONCILE_FAILED_NON_FATAL error=%s", exc)
        try:
            from services.artifact_registry import reconcile_artifact_registry
            await asyncio.to_thread(reconcile_artifact_registry)
        except Exception as exc:
            logger.warning("ARTIFACT_STATUS_ARTIFACT_RECONCILE_FAILED_NON_FATAL error=%s", exc)
        s = get_jobs_summary(limit=20)
        return {"ok": True, "reconcile": reconcile_summary, **s}

    @app.post("/api/artifacts/open")
    async def api_artifact_open(payload: dict = Body(default={})):
        artifact_id = str(payload.get("artifact_id") or "").strip()
        req_path = str(payload.get("path") or "").strip()
        open_mode = str(payload.get("mode") or "file").strip().lower()
        resolved_path = ""
        if artifact_id:
            art = get_artifact_by_id(artifact_id)
            if not art:
                raise HTTPException(status_code=404, detail="artifact_not_found")
            resolved_path = str(art.get("file_path") or "")
        elif req_path:
            if not is_registered_artifact_path(req_path):
                raise HTTPException(status_code=403, detail="path_not_registered")
            resolved_path = req_path
        else:
            raise HTTPException(status_code=400, detail="artifact_id_or_path_required")
        result = open_folder(resolved_path) if open_mode == "folder" else open_file(resolved_path)
        if not bool(result.get("ok")):
            raise HTTPException(status_code=400, detail=str(result.get("error") or "open_failed"))
        return {"ok": True, "result": result}
        throttle_payload = dict(
            telemetry.get("throttle")
            or {
                "count_last_hour": 0,
                "limit": 5,
                "remaining": 5,
                "can_speak": True,
            }
        )
        throttle_remaining = int(throttle_payload.get("remaining", 0) or 0)
        throttle_limit = int(throttle_payload.get("limit", 5) or 5)
        throttle_payload["label"] = f"{max(0, throttle_remaining)}/{throttle_limit} proativos restantes"

        brain_status = "offline"
        if state.get("functional_ready"):
            brain_status = "online"
        elif state.get("boot_recovery_active") or state.get("room_connecting"):
            brain_status = "recovering"
        elif state.get("backend_ready"):
            brain_status = "starting"

        livekit_runtime_status = str(state.get("livekit_status") or "unknown").strip().lower() or "unknown"
        selected_provider = LIVEKIT_SELECTED_PROVIDER
        selected_provider_kind = str(getattr(selected_provider, "kind", "") or "").strip().lower()
        selected_provider_url = str(getattr(selected_provider, "url", "") or "").strip()
        livekit_local_running = is_livekit_running()
        cloud_provider_ready = selected_provider_kind == "cloud" and livekit_runtime_status in {
            "ready", "ok", "connected"
        }
        livekit_connected = livekit_local_running or cloud_provider_ready
        livekit_status = "connected" if livekit_connected else "disconnected"
        if not livekit_connected and (state.get("room_connecting") or state.get("boot_recovery_active")):
            livekit_status = "connecting"

        livekit_current_slot = state.get("livekit_current_slot")
        livekit_total_slots = int(state.get("livekit_total_slots") or LIVEKIT_MANAGER.total_slots or 4)
        livekit_key_label = _format_livekit_key(
            int(livekit_current_slot) if livekit_current_slot is not None else None,
            livekit_total_slots,
        )
        livekit_runtime_label = f"LiveKit: {livekit_key_label} · {_livekit_status_label(livekit_runtime_status)}"

        tts_status = "offline"
        if int(key_report.get("available_tts_keys", 0) or 0) > 0:
            tts_status = "online" if not state.get("boot_recovery_active") else "recovering"
        elif key_report.get("configured_keys"):
            tts_status = "quota"

        total_keys = int(key_report.get("usable_keys", 0) or key_report.get("configured_keys", 0) or 0)
        current_tts_index = key_report.get("current_tts_index")
        current_text_index = key_report.get("current_text_index")
        active_gemini_slot = key_report.get("gemini_current_slot")
        if active_gemini_slot is None:
            active_gemini_index = current_text_index if current_text_index is not None else current_tts_index
            active_gemini_slot = (int(active_gemini_index) + 1) if active_gemini_index is not None else None
        gemini_slot_label = (
            f"{int(active_gemini_slot)}/{total_keys}"
            if active_gemini_slot is not None and total_keys
            else f"-/{max(total_keys, 4)}"
        )
        voice_status_label = {
            "online": "Online",
            "recovering": "Recuperando",
            "quota": "Quota esgotada",
        }.get(tts_status, "Offline")
        overall = "healthy" if (livekit_connected and total_keys > 0) else "degraded"

        return {
            "timestamp": time.time(),
            "brain": {
                "status": brain_status,
                "label": "Cerebro: " + {
                    "online": "Online",
                    "recovering": "Recuperando",
                    "starting": "Inicializando",
                }.get(brain_status, "Offline"),
            },
            "livekit": {
                "status": livekit_status,
                "mode": "selfhost" if selected_provider_kind == "local" else "cloud",
                "url": selected_provider_url or LIVEKIT_URL or "ws://localhost:7880",
                "label": "LiveKit: " + {
                    "connected": (
                        "Conectado (Local)"
                        if selected_provider_kind == "local"
                        else "Conectado (Cloud)"
                    ),
                    "connecting": "Reconectando",
                }.get(livekit_status, "Desconectado"),
            },
            "livekit_key": {
                "status": "error" if livekit_runtime_status == "auth_failed" else (
                    "quota" if livekit_runtime_status == "quota_exceeded" else (
                    "offline" if livekit_runtime_status == "all_failed" else (
                        "starting" if livekit_runtime_status == "starting" else "online"
                    )
                )),
                "label": livekit_runtime_label,
                "current_slot": livekit_current_slot,
                "total_slots": livekit_total_slots,
            },
            "voice_tts": {
                "status": tts_status,
                "label": f"Voz: {voice_status_label}",
            },
            "voice": {
                "status": tts_status,
                "label": voice_status_label,
            },
            "gemini": {
                "status": "online" if total_keys > 0 else "offline",
                "gemini_key": gemini_slot_label,
                "gemini_current_slot": int(active_gemini_slot) if active_gemini_slot is not None else None,
                "gemini_total_slots": max(total_keys, 4),
            },
            "current_key": {
                "tts_index": current_tts_index,
                "text_index": current_text_index,
                "label": f"API {gemini_slot_label}" if total_keys else "API N/A",
            },
            "throttle": throttle_payload,
            "economy": telemetry.get("economy") or {},
            "silence_window": telemetry.get("silence_window") or {},
            "watchdog": telemetry.get("watchdog") or {
                "restart_count": _agent_restart_count,
                "last_restart": _agent_last_restart_at or None,
                "is_running": bool(_agent_proc is not None),
                "is_alive": bool(state.get("backend_ready")),
            },
            "health": telemetry.get("health") or {},
            "voice_mode_status": {
                "mode": str(state.get("voice_mode") or "voice"),
                "restart_required": bool(state.get("restart_required_for_voice")),
                "label": (
                    "Voz pausada — reinicie a sessao"
                    if bool(state.get("restart_required_for_voice"))
                    else (
                        "Modo texto"
                        if str(state.get("voice_mode") or "voice") == "text_only"
                        else ("Voz indisponivel" if str(state.get("voice_mode") or "voice") == "unavailable" else "Modo voz")
                    )
                ),
            },
            "livekit_warning": {
                "visible": bool(state.get("restart_required_for_voice")),
                "text": str(state.get("livekit_message") or "").strip(),
            },
            "overall": overall,
            "state": state,
            "keys": key_report,
        }

    @app.get("/state")
    async def state():
        with _runtime_lock:
            state = _runtime_readiness.as_dict()
        session_debug = _session_debug_snapshot()
        session_state = {
            "room_connected": bool(state.get("room_connected")),
            "ui_room_connected": bool(session_debug.get("ui_room_connected")),
            "runtime_ready": bool(state.get("runtime_ready")),
            "ui_session_ready": bool(state.get("ui_session_ready")),
            "boot_phase": str(state.get("boot_phase") or state.get("boot_state") or "unknown"),
            "failure_reason": str(state.get("failure_reason") or state.get("error") or ""),
            "room_name": str(state.get("room_name") or ""),
            "requested_room_name": str(state.get("requested_room_name") or ""),
            "session_failed": bool(state.get("session_failed")),
            "message": str(state.get("message") or ""),
            "error": str(state.get("error") or ""),
            "functional_ready": bool(state.get("functional_ready")),
            "backend_ready": bool(state.get("backend_ready")),
            "last_event": str(session_debug.get("last_event") or state.get("event") or ""),
            "last_message": str(session_debug.get("last_message") or state.get("message") or ""),
            "last_error": str(session_debug.get("last_error") or state.get("error") or ""),
            "selfhost_dispatch_mode": str(session_debug.get("selfhost_dispatch_mode") or ""),
            "token_issued": bool(session_debug.get("token_issued")),
        }
        return {
            "success": True,
            "session": session_state,
            "livekit_key": state.get("livekit_key"),
            "livekit_current_slot": state.get("livekit_current_slot"),
            "livekit_total_slots": state.get("livekit_total_slots"),
            "livekit_status": state.get("livekit_status"),
            "voice_mode": state.get("voice_mode"),
            "restart_required_for_voice": bool(state.get("restart_required_for_voice")),
            "livekit_message": state.get("livekit_message"),
            "data": state,
            "error": session_state.get("failure_reason") or None,
        }

    @app.post("/api/debug/voice-marker")
    async def voice_marker(payload: dict = Body(default={})):
        label = str(payload.get("label") or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Campo 'label' ausente.")
        marker = f"[VOICE TEST] START {label}"
        _append_voice_validation_log(marker)
        print(marker, flush=True)
        return {"success": True, "data": {"label": label, "marker": marker}, "error": None}

    @app.get("/api/whatsapp/enabled")
    async def wa_enabled_get():
        cfg = load_whatsapp_runtime_config()
        enabled = bool(is_optional_enabled("whatsapp") and cfg.get("enabled"))
        # FASE 2B.2/C1: optional_status_payload traz "enabled" = capability (flag do
        # kira_features.json), que NAO e o runtime. Espalhamos PRIMEIRO e sobrescrevemos
        # com o enabled/mode reais para a UI nao mostrar "LIGADO" com runtime desligado.
        return {"success": True, "data": {**optional_status_payload("whatsapp"), "enabled": enabled, "mode": cfg.get("mode", "assistant")}, "error": None}

    @app.post("/api/whatsapp/enabled")
    async def wa_enabled_set(payload: dict):
        requested = payload.get("enabled", payload.get("whatsapp_enabled", None))
        if requested is None:
            raise HTTPException(status_code=400, detail="Campo 'enabled' ausente.")
        next_enabled = bool(requested)
        logger.info("WHATSAPP_TOGGLE_ENABLE_REQUEST" if next_enabled else "WHATSAPP_TOGGLE_DISABLE_REQUEST")
        if next_enabled and not is_optional_enabled("whatsapp"):
            # O interruptor da aba WhatsApp é o único: ligar aqui também libera o módulo.
            from services import kira_settings

            await asyncio.to_thread(kira_settings.save, {"modules": {"whatsapp": True}})
        logger.info("[WHATSAPP] Alterando enabled para: %s", next_enabled)
        cfg = set_whatsapp_enabled(next_enabled)
        if next_enabled:
            started = await asyncio.to_thread(ensure_whatsapp_started)
            if not started:
                runtime = get_whatsapp_runtime_state()
                return JSONResponse(
                    {
                        "success": False,
                        "data": {"enabled": False, "runtime": runtime},
                        "error": "Erro ao iniciar o subsistema do WhatsApp.",
                    },
                    status_code=500,
                )
            bridge_status = await asyncio.to_thread(_wait_for_whatsapp_bridge_ready, 8.0, 0.5)
            if bridge_status:
                logger.info("[WHATSAPP] Bridge disponivel em 5050")
                logger.info("WHATSAPP_BRIDGE_READY port=5050")
                _set_whatsapp_runtime_state(
                    str(bridge_status.get("session_state") or bridge_status.get("state") or "bridge_online"),
                    last_error=None,
                    bridge_available=True,
                )
            else:
                logger.warning("[WHATSAPP] Bridge ainda nao respondeu apos habilitar.")
        else:
            ensure_whatsapp_stopped()
        runtime = get_whatsapp_runtime_state()
        return {"success": True, "data": {"enabled": bool(cfg.get("enabled")), "runtime": runtime}, "error": None}

    @app.get("/api/whatsapp/mode")
    async def wa_mode_get():
        mode = get_whatsapp_mode()
        logger.info("WHATSAPP_MODE_GET mode=%s", mode)
        logger.info("WHATSAPP_MODE_ACTIVE mode=%s", mode)
        return {"success": True, "data": {"mode": mode}, "error": None}

    @app.post("/api/whatsapp/mode")
    async def wa_mode_set(payload: dict):
        mode = str(payload.get("mode", "assistant")).strip().lower() or "assistant"
        if mode not in {"silent", "assistant", "secretary"}:
            raise HTTPException(status_code=400, detail="Modo invalido.")
        saved = set_whatsapp_mode(mode)
        logger.info("WHATSAPP_MODE_SET mode=%s", saved)
        logger.info("WHATSAPP_MODE_ACTIVE mode=%s", saved)
        return {"success": True, "data": {"mode": saved}, "error": None}

    @app.get("/api/whatsapp/runtime-state")
    async def wa_runtime_state():
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": optional_status_payload("whatsapp"), "error": None}
        return {"success": True, "data": get_whatsapp_runtime_state(), "error": None}

    @app.get("/api/whatsapp/status")
    async def wa_status():
        if not is_optional_enabled("whatsapp"):
            logger.info("WHATSAPP_DISABLED_SKIPPED reason=config source=status_endpoint")
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": optional_status_payload("whatsapp"), "error": None}
        # FASE 2B/C1: fonte unica de verdade — mesmo payload de /runtime-state (status real do processo+bridge).
        runtime = await asyncio.to_thread(get_whatsapp_runtime_state)
        return {
            "success": True,
            "data": runtime,
            "state": runtime.get("state"),
            "status": runtime.get("state"),
            "bridge_status": runtime.get("bridge_status", runtime.get("state")),
            "error": None,
        }

    @app.post("/api/whatsapp/prepare-message")
    async def wa_prepare_message(payload: dict):
        from services.whatsapp_runtime_service import prepare_message
        try:
            from agent.core.whatsapp_outbound import (
                infer_whatsapp_message_style,
                suggest_clarification_question,
            )
        except Exception:
            infer_whatsapp_message_style = None  # type: ignore[assignment]
            suggest_clarification_question = None  # type: ignore[assignment]

        raw_text = str(
            payload.get("raw_text")
            or payload.get("original_text")
            or payload.get("text")
            or ""
        )
        result = prepare_message(
            str(payload.get("contact") or payload.get("to") or ""),
            str(payload.get("message") or payload.get("text") or ""),
            resolver=resolve_whatsapp_contact,
            starter=ensure_whatsapp_started,
            raw_text=raw_text,
            # Texto digitado na aba ("Enviar rápido") sai como o Chefe escreveu: sem adivinhar estilo nem perguntar.
            style_inferer=None if payload.get("verbatim") is True else infer_whatsapp_message_style,
            question_suggester=suggest_clarification_question,
        )
        return {"success": bool(result.get("ok")), "data": result, "error": result.get("error")}

    @app.post("/api/whatsapp/confirm-send")
    async def wa_confirm_send(payload: dict):
        from agent.integrations.whatsapp_runtime import send_whatsapp_message
        from services.whatsapp_runtime_service import confirm_send

        token = str(payload.get("confirm_token") or payload.get("token") or "")

        async def _sender(contact: str, message: str) -> dict:
            return await send_whatsapp_message(contact, message, source="api_confirmation")

        # FASE 2C: passa o estilo para o dedupe/registro.
        style = str(payload.get("style") or "")
        result = await confirm_send(token, sender=_sender, style=style)
        return {"success": bool(result.get("ok")), "data": result, "error": result.get("error")}

    @app.post("/api/whatsapp/cancel-send")
    async def wa_cancel_send(payload: dict):
        from services.whatsapp_runtime_service import cancel_confirmation

        result = cancel_confirmation(str(payload.get("confirm_token") or payload.get("token") or ""))
        return {"success": bool(result.get("ok")), "data": result, "error": result.get("error")}

    @app.get("/api/whatsapp/last-send-status")
    async def wa_last_send_status():
        # FASE 2C: "deu?/foi?/enviou?" — consulta status do ultimo envio/pending; NUNCA reenvia.
        from services.whatsapp_runtime_service import get_last_send_status

        logger.info("WHATSAPP_STATUS_QUERY_NO_RESEND")
        return {"success": True, "data": get_last_send_status(), "error": None}

    @app.get("/api/system-vitals")
    async def system_vitals(active: bool = False, temporary: bool = False, force: bool = False):
        from services.feature_runtime_registry import mark_feature_state
        from services.system_vitals_runtime_service import collect_system_vitals, get_system_vitals_status

        if not is_optional_enabled("system_vitals"):
            logger.info("SYSTEM_VITALS_DISABLED_SKIPPED reason=config source=status_endpoint")
            mark_feature_state("system_vitals", "off", reason="module_disabled")
            return {
                "enabled": False,
                "status": "disabled",
                "critical": False,
                "message": "system vitals disabled by config",
                "data": optional_status_payload("system_vitals"),
            }
        if not active and not temporary and not force:
            mark_feature_state("system_vitals", "off", reason="panel_hidden")
            if KIRA_BOOT_MINIMAL:
                logger.info("SYSTEM_VITALS_BOOT_SKIPPED reason=boot_minimal")
            else:
                logger.info("SYSTEM_VITALS_POLLING_SKIPPED reason=idle")
            return get_system_vitals_status(light=True)
        try:
            interval_s = 5.0 if temporary else 10.0
            reason = "temporary" if temporary else "panel_visible"
            payload = await collect_system_vitals(
                active=True,
                reason=reason,
                collector=lambda: get_system_vitals(
                    min_interval_seconds=interval_s,
                    force=bool(force),
                    scope="temporary" if temporary else "global",
                ),
            )
            logger.info(
                "SYSTEM_VITALS_ENDPOINT_SAMPLE scope=%s interval_s=%.1f force=%s",
                "temporary" if temporary else "global",
                interval_s,
                str(bool(force)).lower(),
            )
            return payload
        except Exception as exc:
            logger.info("SYSTEM_VITALS_COLLECT_FAILED_NON_FATAL reason=%s", exc)
            return optional_endpoint_error("system_vitals", exc) | {"stale": True}

    @app.get("/api/system-vitals/process")
    async def system_vitals_process(name: str = ""):
        if not is_optional_enabled("system_vitals"):
            logger.info("SYSTEM_VITALS_DISABLED_SKIPPED reason=config source=process_endpoint")
            return {
                "enabled": False,
                "status": "disabled",
                "critical": False,
                "message": "system vitals disabled by config",
            }
        payload = get_process_vitals(name)
        logger.info("SYSTEM_VITALS_ENDPOINT_PROCESS query=%s matched=%s", name, str(bool(payload.get("matched"))).lower())
        return payload

    @app.post("/api/system-vitals/stop")
    async def system_vitals_stop(payload: dict = Body(default={})):
        from services.system_vitals_runtime_service import stop_system_vitals

        reason = str(payload.get("reason") or "panel_hidden").strip() or "panel_hidden"
        result = await stop_system_vitals(reason)
        return {"success": True, "data": result, "error": None}

    @app.post("/api/system-vitals/temporary-session")
    async def system_vitals_temporary_session(payload: dict = Body(default={})):
        if not is_optional_enabled("system_vitals"):
            logger.info("SYSTEM_VITALS_DISABLED_SKIPPED reason=config source=temporary_session_endpoint")
            return {
                "ok": True,
                "enabled": False,
                "status": "disabled",
                "critical": False,
                "message": "system vitals disabled by config",
            }
        duration_ms = int(payload.get("duration_ms") or 60000)
        interval_ms = int(payload.get("interval_ms") or 5000)
        show_panel = bool(payload.get("show_panel", True))
        if show_panel:
            _ui_panel_visibility_state["panels"]["system_vitals"] = True
            _ui_panel_visibility_state["version"] = int(_ui_panel_visibility_state["version"]) + 1
        else:
            logger.info("SYSTEM_VITALS_AUTO_SHOW_BLOCKED reason=boot_or_lazy_load")
            logger.info("SYSTEM_VITALS_VISIBILITY_PRESERVED visible=false")
        await transcription_bus.broadcast({
            "type": "system_vitals_temp_session",
            "panel": "system_vitals",
            "visible": show_panel,
            "duration_ms": duration_ms,
            "interval_ms": interval_ms,
            "version": _ui_panel_visibility_state["version"],
        })
        return {"ok": True, "enabled": True, "duration_ms": duration_ms, "interval_ms": interval_ms}

    @app.get("/api/manus/status")
    async def manus_status():
        from services.manus_runtime_service import get_manus_status

        if not is_optional_enabled("manus"):
            logger.info("MANUS_DISABLED_SKIPPED reason=config source=status_endpoint")
            return {
                "success": True,
                "enabled": False,
                "status": "disabled",
                "critical": False,
                "message": "module disabled by config",
                "data": optional_status_payload("manus"),
                "error": None,
            }
        return {
            "success": True,
            "enabled": True,
            "status": "idle",
            "critical": False,
            "message": "manus enabled; lazy load on task",
            "data": {**optional_status_payload("manus"), **get_manus_status()},
            "error": None,
        }

    @app.get("/api/whatsapp/overview")
    async def wa_overview(request: Request):
        """Aba WhatsApp: conta, números de hoje, conversas recentes (quem e quando, sem o texto) e contatos da Kira.

        Lê /messages/recent do bridge, que não esvazia nada (o /messages esvazia a caixa dos avisos).
        """
        from services.local_request import is_local_request
        from services.whatsapp_overview import build_overview

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if not is_optional_enabled("whatsapp") or not is_whatsapp_enabled():
            return {"success": True, "data": {"enabled": False}, "error": None}
        status = await asyncio.to_thread(_read_whatsapp_bridge_status)
        messages: list = []
        if status.get("connected"):
            try:
                import httpx

                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get("http://127.0.0.1:5050/messages/recent", params={"limit": 50}, headers=_whatsapp_bridge_headers())
                    if resp.status_code == 200:
                        messages = resp.json().get("messages") or []
            except Exception:
                messages = []
        contacts = load_whatsapp_contacts().get("contacts") or []
        overview = build_overview(status, messages, contacts, mode=get_whatsapp_mode(), now=datetime.now().astimezone())
        overview["secretary"] = app.state.whatsapp_secretary.status()  # quantas respondeu hoje e com quem (sem o texto)
        return {"success": True, "data": {"enabled": True, **overview}, "error": None}

    @app.get("/api/whatsapp/messages")
    async def wa_messages():
        """Proxy para buscar novas mensagens do WhatsApp Bridge."""
        if not is_optional_enabled("whatsapp"):
            logger.info("WHATSAPP_DISABLED_SKIPPED reason=config source=messages_endpoint")
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": {"messages": []}, "error": None}
        if not is_whatsapp_enabled() or not _is_whatsapp_process_running():
            return {"success": True, "data": {"messages": []}, "error": None}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://127.0.0.1:5050/messages/new", headers=_whatsapp_bridge_headers())
                data = resp.json() if resp.status_code == 200 else {"messages": []}
                return {"success": True, "data": data, "error": None}
        except Exception:
            return {"success": True, "data": {"messages": []}, "error": None}

    @app.get("/api/whatsapp/messages/agent")
    async def wa_messages_agent():
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": {"messages": []}, "error": None}
        if not is_whatsapp_enabled() or not _is_whatsapp_process_running():
            return {"success": True, "data": {"messages": []}, "error": None}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://127.0.0.1:5050/messages/new/agent", headers=_whatsapp_bridge_headers())
                data = resp.json() if resp.status_code == 200 else {"messages": []}
                return {"success": True, "data": data, "error": None}
        except Exception:
            return {"success": True, "data": {"messages": []}, "error": None}

    @app.get("/api/whatsapp/moods")
    async def wa_moods_get(request: Request):
        """Personalidade da Kira com cada contato (aba WhatsApp)."""
        from services.local_request import is_local_request
        from services.contact_mood import PRESETS, all_moods
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        return {"ok": True, "moods": all_moods(), "presets": {k: v[0] for k, v in PRESETS.items()}}

    @app.post("/api/whatsapp/mood")
    async def wa_mood_set(request: Request, payload: dict = Body(default={})):
        from services.local_request import is_local_request
        from services.contact_mood import describe, key, set_manual
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        number = str(payload.get("number") or "")
        if not key(number):
            return {"ok": False, "error": "numero"}
        try:
            rec = set_manual(number, str(payload.get("preset") or ""))
        except ValueError:
            return {"ok": False, "error": "preset"}
        return {"ok": True, "key": key(number), "mood": describe(rec)}

    @app.get("/api/whatsapp/contacts")
    async def wa_contacts_get():
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": {"contacts": []}, "error": None}
        payload = load_whatsapp_contacts()
        return {
            "success": True,
            "data": {
                "contacts": payload.get("contacts") or [],
                "version": payload.get("version") or "",
                "updated_at": payload.get("updated_at") or "",
            },
            "error": None,
        }

    @app.post("/api/whatsapp/contacts")
    async def wa_contacts_post(request: Request, payload: dict):
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": optional_status_payload("whatsapp"), "error": None}
        name = " ".join(str((payload or {}).get("name") or "").split())[:40]
        digits = re.sub(r"\D", "", str((payload or {}).get("number") or (payload or {}).get("phone") or ""))
        # a aba já manda com o código do país (DDD + número ganha o 55 lá)
        saved_as = " ".join(str((payload or {}).get("whatsapp_name") or "").split())[:40]
        # sem número vale se tiver o nome salvo no celular: a Kira pesquisa por ele no WhatsApp
        if not name or (digits and not 11 <= len(digits) <= 15) or not (digits or saved_as):
            return {"success": False, "data": None, "error": "Escreva o nome e o nome salvo no celular ou o número com DDD."}
        saved = await asyncio.to_thread(upsert_whatsapp_contact, {**(payload or {}), "name": name, "number": digits})
        latest = load_whatsapp_contacts()
        return {
            "success": True,
            "data": {
                "contact": saved,
                "contacts": latest.get("contacts") or [],
                "version": latest.get("version") or "",
                "message": f'Contato "{saved.get("name") or "Contato"}" salvo com sucesso.',
            },
            "error": None,
        }

    @app.post("/api/ui/windows/transfer")
    async def ui_windows_transfer(request: Request, payload: dict = Body(default={})):
        """Painel solto: se no ponto da tela há outra janela da Kira (ao lado ou por cima), o painel passa para ela."""
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        try:
            x, y = int(body.get("screen_x")), int(body.get("screen_y"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="ponto invalido")
        panel_id = str(body.get("panel_id") or "")[:64]
        target = await asyncio.to_thread(app.state.window_registry.window_at, x, y)
        moved = bool(target) and await app.state.panel_registry.move(panel_id, target)
        if moved:  # todo arrasto pergunta; só a passagem de fato vai para o log
            logger.info("UI_PANEL_TRANSFER panel=%s target=%s", panel_id, target)
        return {"ok": True, "moved": moved, "window": target}

    @app.post("/api/whatsapp/contacts/delete")
    async def wa_contacts_delete(request: Request, payload: dict = Body(default={})):
        """Aba WhatsApp: tira um contato da agenda da Kira (ela deixa de entender o nome e, na Secretária, de responder)."""
        from services.local_request import is_local_request
        from services.whatsapp_data import delete_whatsapp_contact

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        removed = await asyncio.to_thread(delete_whatsapp_contact, (payload or {}).get("id"))
        return {"success": removed, "data": {"removed": removed}, "error": None if removed else "Contato não encontrado."}

    @app.get("/api/whatsapp/resolve-contact")
    async def wa_resolve_contact(query: str = ""):
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": None, "error": None}
        resolved = resolve_whatsapp_contact(query)
        return {
            "success": True,
            "data": resolved,
            "error": None,
        }

    # TEMPORARIAMENTE DESABILITADO - Sistema de contatos
    # @app.post("/api/contacts/add")
    # async def contacts_add(payload: dict):
    #     ...
    #
    # @app.get("/api/contacts/list")
    # async def contacts_list():
    #     ...
    #
    # @app.post("/api/contacts/delete")
    # async def contacts_delete(payload: dict):
    #     ...

    @app.get("/api/whatsapp/logs")
    async def wa_logs():
        if not is_optional_enabled("whatsapp"):
            logger.info("WHATSAPP_DISABLED_SKIPPED reason=config source=logs_endpoint")
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": {"logs": [], "version": str(int(time.time() * 1000))}, "error": None}
        bridge_logs = _read_whatsapp_bridge_log()
        send_logs = [
            {
                "id": str(item.get("request_id") or f"send-log-{index}"),
                "type": "success" if str(item.get("status") or "") == "sent" else "info",
                "message": (
                    f'{item.get("status")}: {item.get("contact")} -> {item.get("message")}'
                    if item.get("contact")
                    else str(item.get("message") or "")
                ),
                "timestamp": str(item.get("sent_at") or item.get("created_at") or ""),
            }
            for index, item in enumerate(load_whatsapp_send_records(limit=80), start=1)
        ]
        combined = [*bridge_logs, *send_logs][-120:]
        return {
            "success": True,
            "data": {
                "logs": combined,
                "version": str(int(time.time() * 1000)),
            },
            "error": None,
        }

    @app.get("/api/whatsapp/session/events")
    async def wa_session_events():
        if not is_optional_enabled("whatsapp"):
            return {"success": True, "enabled": False, "status": "disabled", "critical": False, "message": "module disabled by config", "data": {"logs": [], "version": str(int(time.time() * 1000))}, "error": None}
        runtime = get_whatsapp_runtime_state()
        details = runtime.get("details") if isinstance(runtime.get("details"), dict) else {}
        logs = []
        if details:
            for key, value in details.items():
                logs.append(
                    {
                        "id": f"session-{key}",
                        "type": "info",
                        "message": f"{key}: {value}",
                        "timestamp": "",
                    }
                )
        return {
            "success": True,
            "data": {
                "logs": logs[-80:],
                "version": str(int(time.time() * 1000)),
            },
            "error": None,
        }

    @app.post("/api/whatsapp/logout")
    async def wa_logout():
        """Desconecta a conta do WhatsApp da Kira (o bridge volta a mostrar um QR novo)."""
        logger.info("WHATSAPP_LOGOUT_REQUEST source=ui")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post("http://127.0.0.1:5050/logout", headers=_whatsapp_bridge_headers())
            return JSONResponse(resp.json() if resp.content else {}, status_code=resp.status_code)
        except Exception as exc:
            logger.error("[WHATSAPP] Erro logout: %s", exc)
            return JSONResponse({"success": False, "error": "O WhatsApp da Kira não está rodando."}, status_code=503)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/livekit/health")
    async def livekit_health():
        logger.info("[HEALTH] Verificando LiveKit server...")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                reachable = sock.connect_ex(("127.0.0.1", int(LIVEKIT_PORT))) == 0
            if not reachable:
                logger.error("[HEALTH] LiveKit nao responde na porta %s", LIVEKIT_PORT)
                raise HTTPException(
                    status_code=503,
                    detail=f"LiveKit nao responde em 127.0.0.1:{LIVEKIT_PORT}",
                )
            logger.info("[HEALTH] LiveKit respondendo na porta %s", LIVEKIT_PORT)
            return {
                "status": "ok",
                "livekit_reachable": True,
                "port": int(LIVEKIT_PORT),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[HEALTH] Erro ao verificar LiveKit: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/vision/state")
    async def vision_state():
        try:
            state = await _get_agent_vision_state()
        except Exception as exc:
            logger.warning("[VISION_MODE] state_request_failed reason=%s", exc)
            state = _apply_effective_vision_state(
                {
                    "vision_available": False,
                    "vision_status": "unavailable",
                    "error": "vision_control_unreachable",
                    "detail": str(exc),
                },
                status_failed=True,
            )
            return {
                "success": True,
                "data": state,
                "error": None,
            }
        return {"success": True, "data": state, "error": None}

    @app.get("/api/vision/status")
    async def vision_status():
        try:
            state = await _get_agent_vision_state()
        except Exception as exc:
            logger.warning("[VISION_MODE] status_request_failed reason=%s", exc)
            logger.warning("[VISION_DEGRADED] vision_control_unreachable ignored_for_boot=true")
            state = _apply_effective_vision_state(
                {
                    "vision_available": False,
                    "vision_status": "unavailable",
                    "error": "vision_control_unreachable",
                    "detail": str(exc),
                },
                status_failed=True,
            )
            return {
                "success": True,
                "data": state,
                "error": None,
            }
        return {"success": True, "data": state, "error": None}

    @app.post("/api/vision/livekit-screen-share/started")
    async def livekit_screen_share_started(request: Request):
        from services.vision_livekit_state import start_vision_livekit

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        track_id = str(payload.get("track_id") or "").strip()
        settings = dict(payload.get("settings") or {})
        logger.info(
            "[LIVEKIT VISION] screen_share_started track_id=%s settings=%s",
            track_id or "none",
            settings,
        )
        logger.info("VISION_SCREEN_SHARE_REQUESTED source=ui")
        logger.info("VOICE_PIPELINE_NOT_RESTARTED reason=vision_toggle")
        logger.info("GEMINI_SESSION_NOT_RESTARTED reason=vision_toggle")
        data = start_vision_livekit(reason="user_enabled")
        from services.screen_share_picker import show_share_bar

        show_share_bar()  # barra "A Kira está vendo…" com "Parar compartilhamento", como a do Chrome
        data["track_id"] = track_id
        data["track_settings"] = settings
        return {"success": True, "data": data, "error": None}

    @app.get("/api/vision/share-choice")
    async def vision_share_choice():
        """O que o usuário escolheu mostrar (todos os monitores, um monitor ou uma janela)."""
        from services.screen_share_picker import current_choice

        return current_choice()

    @app.post("/api/vision/livekit-screen-share/stopped")
    async def livekit_screen_share_stopped(request: Request):
        from services.screen_share_picker import hide_share_bar, reset_choice
        from services.vision_livekit_state import stop_vision_livekit

        reset_choice()
        hide_share_bar()

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        reason = str(payload.get("reason") or "user_disabled").strip().lower()
        ended_by_browser = reason in {"browser_ended", "track_ended"}
        logger.info("[LIVEKIT VISION] screen_share_stopped reason=%s", reason)
        if ended_by_browser:
            logger.info("VISION_SCREEN_SHARE_ENDED_BY_BROWSER")
        if _is_pc_operator_executing_from_status():
            logger.info("VISION_STOP_IGNORED_DURING_PC_OPERATOR")
            try:
                state = await _get_agent_vision_state()
            except Exception:
                state = _apply_effective_vision_state({}, status_failed=True)
            return {"success": True, "data": state, "error": None}
        _vision_effective_state_cache["pending_stop_since"] = time.time()
        _vision_effective_state_cache["pending_stop_reason"] = "screen_share_stopped"
        logger.info("VISION_STOP_PENDING reason=screen_share_stopped")
        last_frame_at = float(_vision_effective_state_cache.get("last_frame_at") or 0.0)
        age_ms = int((time.time() - last_frame_at) * 1000.0) if last_frame_at else None
        recent_frame = bool(last_frame_at and age_ms is not None and age_ms <= int(os.getenv("KIRA_VISION_FRAME_STALE_MS", "5000") or "5000"))
        if recent_frame and not ended_by_browser:
            logger.info("VISION_STOP_CANCELLED_FRAME_RECENT age_ms=%s", age_ms)
            try:
                state = await _get_agent_vision_state()
            except Exception:
                state = _apply_effective_vision_state({}, status_failed=True)
            return {"success": True, "data": state, "error": None}
        if ended_by_browser:
            logger.info("VISION_STATE_RESET reason=track_ended")
        else:
            logger.info("VISION_DISABLED_BY_USER")
        data = stop_vision_livekit(reason="track_ended" if ended_by_browser else "user_disabled")
        return {"success": True, "data": data, "error": None}

    @app.get("/api/vision/active-source")  # BLOCO 6
    async def vision_active_source():
        state = await _get_agent_vision_state()
        screen_share = bool(state.get("livekit_screen_active"))
        mode_enabled = bool(state.get("vision_mode_enabled"))
        source = "livekit_screen_share" if screen_share and mode_enabled else "none"
        return {
            "success": True,
            "source": source,
            "screen_share_active": screen_share,
            "mode_enabled": mode_enabled,
            "screen_share_requested": bool(state.get("screen_share_requested") or mode_enabled or screen_share),
            "screen_share_track_active": bool(state.get("screen_share_track_active") or screen_share),
            "webcam_track_active": bool(state.get("webcam_track_active")),
            "last_screen_frame_at": state.get("last_screen_frame_at") or state.get("last_frame_at"),
            "last_webcam_frame_at": state.get("last_webcam_frame_at"),
            "vision_active_source": source,
            "vision_frames_recent": bool(state.get("vision_frames_recent") or state.get("recent_frame")),
            "vision_effective_active": bool(state.get("vision_effective_active") or state.get("effective_active")),
            "latest_frame_age_ms": state.get("latest_frame_age_ms"),
            "adaptive_fps": state.get("adaptive_fps"),
            "native_video_active": bool(state.get("native_video_active")),
            "visual_context_age_ms": state.get("visual_context_age_ms"),
            "error": None,
        }

    @app.get("/api/vision/monitors")
    async def vision_monitors():
        try:
            monitors = await _get_agent_vision_monitors()
        except Exception as exc:
            logger.warning("[VISION_MODE] monitors_request_failed reason=%s", exc)
            return optional_endpoint_error("vision", exc)
        return {"success": True, "data": monitors, "error": None}

    @app.post("/api/activity/ping")
    async def activity_ping(payload: dict = Body(default={})):
        source = str(payload.get("source") or "unknown").strip() or "unknown"
        try:
            state = await _ping_agent_activity(source)
        except Exception as exc:
            logger.warning("[ACTIVITY_MONITOR] ping_failed source=%s reason=%s", source, exc)
            return {
                "success": True,
                "data": {
                    "activity_available": False,
                    "vision_available": False,
                    "vision_status": "unavailable",
                    "error": "activity_ping_unreachable",
                    "source": source,
                    "detail": str(exc),
                },
                "error": None,
            }
        return {"success": True, "data": state, "error": None}

    @app.post("/api/vision/toggle")
    async def toggle_vision_mode(payload: dict = Body(default={})):
        enabled = bool(payload.get("enabled", False))
        source = str(payload.get("source") or "manual").strip() or "manual"
        monitor_id = payload.get("monitor_id", None)
        if not enabled and "pc_operator" in source.lower():
            logger.warning("PC_OPERATOR_VISION_DISABLE_BLOCKED reason=operator_must_not_toggle_vision")
            try:
                state = await _get_agent_vision_state()
            except Exception:
                state = _apply_effective_vision_state({}, status_failed=True)
            return {"success": True, "data": state, "error": None}
        logger.info(
            "[VISION_MODE] API toggle requested enabled=%s source=%s monitor_id=%s",
            enabled,
            source,
            monitor_id,
        )
        try:
            try:
                state = await _toggle_agent_vision_mode(enabled, source=source, monitor_id=monitor_id)
            except TypeError:
                state = await _toggle_agent_vision_mode(enabled, source=source)
        except Exception as exc:
            logger.warning("[VISION_MODE] toggle_failed enabled=%s reason=%s", enabled, exc)
            return optional_endpoint_error("vision", exc)

        logger.info(
            "[VISION_MODE] API toggle synced enabled=%s capture_active=%s",
            state.get("vision_mode_enabled"),
            state.get("screen_capture_active"),
        )
        return {"success": True, "data": state, "error": None}

    @app.get("/api/assistant/runtime-state")
    async def assistant_runtime_state():
        return {
            "success": True,
            "data": {
                **_get_assistant_runtime_state(),
                "control_enabled": False,
                "action_allowed": False,
                "pc_operator_enabled": False,
                "pc_operator_removed": True,
                "replacement": "browser_agent",
                "message": "PC Operator removido. Use o navegador da Kira para tarefas web.",
            },
            "error": None,
        }

    @app.post("/api/assistant/runtime-state")
    async def assistant_runtime_state_set(payload: dict):
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/assistant/runtime-state")
        return {
            "success": True,
            "data": {
                "pc_operator_enabled": False,
                "pc_operator_removed": True,
                "replacement": "browser_agent",
                "message": "PC Operator removido. Use o navegador da Kira para tarefas web.",
            },
            "error": None,
        }

    @app.post("/api/assistant/control")
    async def assistant_control_set(payload: dict):
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/assistant/control")
        return JSONResponse(
            {
                "success": False,
                "data": {"blocked": True, "reason": "pc_operator_removed"},
                "error": "PC Operator removido desta versao da Kira.",
            },
            status_code=410,
        )

    @app.post("/api/assistant/action")
    async def assistant_action(payload: dict):
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/assistant/action")
        return JSONResponse(
            {
                "success": False,
                "data": {"blocked": True, "reason": "pc_operator_removed"},
                "error": "Controle direto de mouse/teclado removido. Use o navegador da Kira para tarefas web.",
            },
            status_code=410,
        )

    @app.get("/api/kira-ui/state")
    async def kira_ui_state():
        state = _read_kira_ui_state()
        print(f"[KIRA UI] ui_state_after={state}", flush=True)
        return {"success": True, "data": state, "error": None}

    @app.post("/api/kira-ui/control")
    async def kira_ui_control(payload: dict):
        print(f"[KIRA UI] ui_action_received payload={payload}", flush=True)
        try:
            # evaluate_js e a espera pelo estado bloqueiam: fora do event loop do servidor.
            result = await asyncio.to_thread(_dispatch_kira_ui_action, payload)
        except Exception as exc:
            print(f"[KIRA UI] ui_action_failed error={exc}", flush=True)
            return {"success": False, "data": {"ok": False}, "error": str(exc)}

        if result.get("ok"):
            print(
                f"[KIRA UI] ui_action_success action={result.get('action')} resource={result.get('resource')} target={result.get('target')} state_after={result.get('state_after')}",
                flush=True,
            )
            return {"success": True, "data": result, "error": None}
        print(
            f"[KIRA UI] ui_action_failed action={result.get('action')} resource={result.get('resource')} target={result.get('target')} error={result.get('error')}",
            flush=True,
        )
        return {"success": False, "data": result, "error": result.get("error") or "ui_action_failed"}

    # ── Music Module API ───────────────────────────────────────────────

    def _music_error_payload(exc: Exception) -> dict:
        message = str(exc or "music_error").strip() or "music_error"
        return {"success": False, "data": None, "error": message}

    def _music_light_state() -> dict:
        from services.music_runtime_service import get_music_status

        return get_music_status()

    def _ensure_music_components(reason: str = "user_requested") -> None:
        global _youtube_music_module
        global MusicServiceError, MusicStateStore, PlaylistStore, YouTubeMusicService
        global _music_service, _music_playlists, _music_state, _music_components_loaded
        if _music_components_loaded:
            return
        from services.feature_runtime_registry import mark_feature_error, mark_feature_running, mark_feature_starting

        mark_feature_starting("music", reason=reason)
        if not is_optional_enabled("music"):
            mark_feature_error("music", reason="module_disabled")
            _music_components_loaded = True
            return
        try:
            if _youtube_music_module is None:
                _youtube_music_module = safe_import_optional("services.youtube_music_service", service="music")
            MusicServiceError = getattr(_youtube_music_module, "MusicServiceError", RuntimeError)
            MusicStateStore = getattr(_youtube_music_module, "MusicStateStore", None)
            PlaylistStore = getattr(_youtube_music_module, "PlaylistStore", None)
            YouTubeMusicService = getattr(_youtube_music_module, "YouTubeMusicService", None)
            _music_service = run_optional_task("music", YouTubeMusicService, fallback=None, reason="music_service_init_failed") if YouTubeMusicService is not None else None
            _music_playlists = run_optional_task("music", PlaylistStore, MUSIC_PLAYLISTS_PATH, fallback=None, reason="music_playlist_store_init_failed") if PlaylistStore is not None else None
            _music_state = run_optional_task("music", MusicStateStore, MUSIC_STATE_PATH, fallback=None, reason="music_state_store_init_failed") if MusicStateStore is not None else None
            _music_components_loaded = True
            mark_feature_running("music", reason=reason)
        except Exception as exc:
            _music_components_loaded = True
            mark_feature_error("music", reason=str(exc))

    def _music_component(component: object, name: str):
        if component is None and not _music_components_loaded:
            _ensure_music_components(reason="user_requested")
            component = globals().get(f"_{name}")
        if component is None:
            raise MusicServiceError(f"{name}_unavailable")
        return component

    def _sanitize_query(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @app.get("/api/music/search")
    async def music_search_api(q: str, max_results: int = 10):
        query = _sanitize_query(q)
        if not query:
            return {"success": False, "data": None, "error": "query_required"}
        try:
            service = _music_component(_music_service, "music_service")
            results = service.search_music(query, max_results=max_results)
            return {"success": True, "data": {"query": query, "results": results}, "error": None}
        except MusicServiceError as exc:
            print(f"[KIRA MUSIC] youtube_search_error error={exc}", flush=True)
            return _music_error_payload(exc)
        except Exception as exc:
            print(f"[KIRA MUSIC] youtube_search_error error={exc}", flush=True)
            return _music_error_payload(MusicServiceError("Falha interna ao buscar músicas."))

    fullscreen_restore: dict = {}

    def _set_main_window_fullscreen(on: bool) -> dict:
        """Tela cheia da janela da Kira no monitor principal (vídeo do painel); ao sair, volta como estava."""
        window = _webview_win
        try:
            from qtpy.QtGui import QGuiApplication
            from webview.platforms.qt import BrowserView
            view = BrowserView.instances.get(window.uid) if window is not None else None
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
        if view is None:
            return {"ok": False, "error": "window_not_ready"}
        if on and not view.is_fullscreen:
            fullscreen_restore.clear()
            fullscreen_restore.update(maximized=view.isMaximized(), x=window.x, y=window.y, width=window.width, height=window.height)
            if fullscreen_restore["maximized"]:
                window.restore()
            primary = QGuiApplication.primaryScreen().availableGeometry()
            window.move(primary.x() + 40, primary.y() + 40)
            window.toggle_fullscreen()
        elif not on and view.is_fullscreen:
            window.toggle_fullscreen()
            if fullscreen_restore.get("maximized"):
                window.maximize()
            elif fullscreen_restore:
                window.move(fullscreen_restore["x"], fullscreen_restore["y"])
                window.resize(fullscreen_restore["width"], fullscreen_restore["height"])
        return {"ok": True, "fullscreen": bool(on)}

    @app.post("/api/window/fullscreen")
    async def window_fullscreen_api(payload: dict = Body(default={})):
        return await asyncio.to_thread(_set_main_window_fullscreen, bool(payload.get("on")))

    @app.get("/api/music/channel-latest")
    async def music_channel_latest_api(channel: str):
        name = _sanitize_query(channel)
        if not name:
            return {"success": False, "data": None, "error": "channel_required"}
        try:
            service = _music_component(_music_service, "music_service")
            results = await asyncio.to_thread(service.latest_from_channel, name)
            return {"success": True, "data": {"channel": name, "results": results}, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.get("/api/music/video/{video_id}")
    async def music_video_api(video_id: str):
        vid = str(video_id or "").strip()
        if not vid:
            return JSONResponse(
                {"success": False, "data": None, "error": "video_id_required"},
                status_code=400,
            )
        try:
            details = _music_component(_music_service, "music_service").get_video_details([vid]).get(vid)
            if not details:
                # O envelope continua compatível com os demais endpoints de
                # música, mas um recurso ausente deixa de parecer sucesso HTTP.
                return JSONResponse(
                    {"success": False, "data": None, "error": "video_not_found"},
                    status_code=404,
                )
            return {"success": True, "data": details, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.get("/api/music/playlists")
    async def music_playlists_list_api():
        try:
            return {"success": True, "data": {"playlists": _music_component(_music_playlists, "music_playlists").list_playlists()}, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.post("/api/music/playlists")
    async def music_playlists_create_api(payload: dict = Body(default={})):
        name = str(payload.get("name") or "").strip()
        try:
            created = _music_component(_music_playlists, "music_playlists").create_playlist(name)
            return {"success": True, "data": created, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.put("/api/music/playlists/{playlist_id}")
    async def music_playlists_update_api(playlist_id: str, payload: dict = Body(default={})):
        name = payload.get("name")
        try:
            updated = _music_component(_music_playlists, "music_playlists").update_playlist(playlist_id, name=name if name is not None else None)
            return {"success": True, "data": updated, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.delete("/api/music/playlists/{playlist_id}")
    async def music_playlists_delete_api(playlist_id: str):
        try:
            _music_component(_music_playlists, "music_playlists").delete_playlist(playlist_id)
            return {"success": True, "data": {"id": playlist_id}, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.post("/api/music/playlists/{playlist_id}/items")
    async def music_playlist_add_item_api(playlist_id: str, payload: dict = Body(default={})):
        try:
            updated = _music_component(_music_playlists, "music_playlists").add_item(playlist_id, payload)
            return {"success": True, "data": updated, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.delete("/api/music/playlists/{playlist_id}/items/{item_id}")
    async def music_playlist_remove_item_api(playlist_id: str, item_id: str):
        try:
            updated = _music_component(_music_playlists, "music_playlists").remove_item(playlist_id, item_id)
            return {"success": True, "data": updated, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.post("/api/music/play")
    async def music_play_api(payload: dict = Body(default={})):
        query = _sanitize_query(payload.get("query", ""))
        playlist_id = str(payload.get("playlist_id") or "").strip()
        video_id = str(payload.get("video_id") or "").strip()
        chosen: dict | None = None

        try:
            if video_id:
                chosen = _music_component(_music_service, "music_service").get_video_details([video_id]).get(video_id)
                if not chosen:
                    raise MusicServiceError("Vídeo não encontrado.")
            elif playlist_id:
                playlist = _music_component(_music_playlists, "music_playlists").get_playlist(playlist_id)
                items = list(playlist.get("items") or [])
                if not items:
                    raise MusicServiceError("A playlist está vazia.")
                chosen = items[0]
            elif query:
                results = _music_component(_music_service, "music_service").search_music(query, max_results=1)
                if not results:
                    raise MusicServiceError("Nenhum resultado encontrado para a busca.")
                chosen = results[0]
            else:
                raise MusicServiceError("Informe query, video_id ou playlist_id para iniciar a reprodução.")

            state = _music_component(_music_state, "music_state").set_state(
                {
                    "currentVideoId": str(chosen.get("videoId") or ""),
                    "currentTitle": str(chosen.get("title") or ""),
                    "currentChannel": str(chosen.get("channel") or ""),
                    "isPlaying": True,
                    "currentTime": 0,
                    "duration": chosen.get("duration"),
                    "durationSeconds": int(chosen.get("durationSeconds") or 0),
                    "volume": 70,
                    "currentPlaylistId": playlist_id or None,
                    "queue": [chosen],
                    "currentIndex": 0,
                }
            )
            return {"success": True, "data": {"track": chosen, "state": state}, "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.get("/api/music/status")
    async def music_status_api():
        return {"success": True, "data": _music_light_state(), "error": None}

    @app.post("/api/music/tab-active")
    async def music_tab_active_api(payload: dict = Body(default={})):
        from services.music_runtime_service import ensure_music_started, set_music_tab_active

        active = bool(payload.get("active", True))
        state = set_music_tab_active(active)
        if active:
            state = await ensure_music_started("music_tab_opened")
        return {"success": True, "data": state, "error": None}

    @app.post("/api/music/play-runtime")
    async def music_play_runtime_api(payload: dict = Body(default={})):
        from services.music_runtime_service import play_music

        query = _sanitize_query(payload.get("query", "")) or None

        async def _player(q: str | None) -> dict:
            result = await music_play_api({"query": q or "musica"})
            if not bool(result.get("success")):
                return {"ok": False, "error": result.get("error") or "music_play_failed"}
            data = dict(result.get("data") or {})
            return {"ok": True, "track": data.get("track") or {"query": q or "musica"}}

        result = await play_music(query, player=_player)
        return {"success": bool(result.get("ok")), "data": result, "error": result.get("error")}

    @app.post("/api/music/command")
    async def music_command_api(payload: dict = Body(default={})):
        from services.music_runtime_service import (
            next_track,
            pause_music,
            previous_track,
            resume_music,
            set_volume,
            stop_music,
        )

        command = str(payload.get("command") or "").strip().lower()
        value = payload.get("value")
        if command == "pause":
            result = await pause_music()
        elif command in {"resume", "play"}:
            result = await resume_music()
        elif command == "stop":
            result = await stop_music()
        elif command == "next":
            result = await next_track(player=lambda: {"ok": False, "error": "next_not_supported_without_ui"})
        elif command == "previous":
            result = await previous_track(player=lambda: {"ok": False, "error": "previous_not_supported_without_ui"})
        elif command == "set_volume":
            result = await set_volume(int(value if value is not None else 70))
        else:
            result = {"ok": False, "error": "music_command_not_supported", "state": _music_light_state()}
        return {"success": bool(result.get("ok")), "data": result, "error": result.get("error")}

    @app.get("/api/music/state")
    async def music_state_api(active: bool = False):
        if not active:
            return {"success": True, "data": _music_light_state(), "error": None}
        try:
            return {"success": True, "data": _music_component(_music_state, "music_state").get_state(), "error": None}
        except MusicServiceError as exc:
            return _music_error_payload(exc)

    @app.post("/api/music/state")
    async def music_state_update_api(payload: dict = Body(default={})):
        try:
            state = _music_component(_music_state, "music_state").set_state(payload or {})
            try:
                app.state.tv_music(state)
            except Exception:  # TV é extra: nunca atrapalha a música
                pass
            if not bool(state.get("isPlaying")):
                from services.feature_runtime_registry import mark_feature_state
                from services.music_runtime_service import touch_idle

                touch_idle()
                mark_feature_state("music", "paused", reason="not_playing")
            return {"success": True, "data": state, "error": None}
        except Exception as exc:
            return _music_error_payload(exc)

    def _youtube_audio():
        global _youtube_audio_service
        if _youtube_audio_service is None:
            from services.youtube_audio import YoutubeAudio

            _youtube_audio_service = YoutubeAudio(BASE_DIR)
        return _youtube_audio_service

    @app.post("/api/music/youtube-audio")
    async def music_youtube_audio_api(request: Request, payload: dict = Body(default={})):
        """Vídeo que o dono não deixa tocar fora do YouTube (erro 150): o endereço do áudio no próprio YouTube."""
        from services.local_request import is_local_request
        from services.youtube_audio import YoutubeAudioError

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        try:
            info = await _youtube_audio().resolve(payload.get("videoId"), force=payload.get("force") is True)
        except YoutubeAudioError as exc:
            return {"success": False, "data": None, "error": str(exc)}
        return {"success": True, "data": {"streamUrl": info["url"], "duration": info["duration"]}, "error": None}

    @app.post("/api/music/youtube-audio/prefetch")
    async def music_youtube_audio_prefetch_api(request: Request, payload: dict = Body(default={})):
        """Depois da busca: deixa pronto o áudio das faixas que já deram erro 150 (o clique toca na hora)."""
        from services.local_request import is_local_request

        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        ids = payload.get("videoIds") if isinstance(payload.get("videoIds"), list) else []
        return {"success": True, "data": {"started": _youtube_audio().prefetch(ids)}, "error": None}

    def _music_fallback():
        global _music_fallback_service
        if _music_fallback_service is None:
            from services.music_fallback import MusicFallback

            _music_fallback_service = MusicFallback(youtube_audio=_youtube_audio(), base_dir=BASE_DIR)
        return _music_fallback_service

    # Plano B da música: outra versão no YouTube ou a mesma música no SoundCloud (services/music_fallback.py).
    from services.music_fallback_api import create_music_fallback_router

    app.include_router(create_music_fallback_router(_music_fallback))

    @app.get("/api/pc-operator/status")
    @app.get("/api/desktop-operator/status")
    async def desktop_operator_status():
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/desktop-operator/status")
        return {"success": True, "data": {"pc_operator_removed": True, "replacement": "browser_agent"}, "error": None}

    @app.get("/api/pc-operator/config")
    @app.get("/api/desktop-operator/config")
    async def desktop_operator_config_get():
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/desktop-operator/config")
        return {"success": True, "data": {"pc_operator_removed": True, "replacement": "browser_agent"}, "error": None}

    @app.post("/api/pc-operator/config")
    @app.post("/api/desktop-operator/config")
    async def desktop_operator_config_set(payload: dict = Body(default={})):
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/desktop-operator/config")
        return JSONResponse({"success": False, "data": {"blocked": True, "reason": "pc_operator_removed"}, "error": "PC Operator removido desta versao da Kira."}, status_code=410)

    @app.post("/api/pc-operator/stop")
    @app.post("/api/desktop-operator/stop")
    async def desktop_operator_stop():
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/desktop-operator/stop")
        return {"success": True, "data": {"pc_operator_removed": True, "stopped": True}, "error": None}

    @app.get("/api/pc-operator/monitors")
    @app.get("/api/desktop-operator/monitors")
    async def desktop_operator_monitors():
        logger.info("PC_OPERATOR_REMOVED_ENDPOINT_CALLED route=/api/desktop-operator/monitors")
        return JSONResponse({"success": False, "data": {"blocked": True, "reason": "pc_operator_removed"}, "error": "Controle direto de monitores removido."}, status_code=410)

    return app


def run_server():
    global _server_start_error
    import uvicorn
    boot_orchestrator.set_phase(BootPhase.BACKEND_STARTING, details="run_server")
    logger.info("[BACKEND] Iniciando servidor HTTP host=127.0.0.1 port=%s", PORT)
    try:
        app = build_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=PORT,
            log_level="info",
            access_log=False,
        )
        logger.info("UVICORN_ACCESS_LOG_DISABLED reason=polling_noise")
        server = uvicorn.Server(config)

        original_startup = server.startup

        async def startup_with_event(*args, **kwargs):
            await original_startup(*args, **kwargs)
            logger.info("[BACKEND] Servidor HTTP pronto em http://127.0.0.1:%s", PORT)
            _server_ready.set()
            _update_runtime_readiness(
                backend_ready=True,
                backend_status="ready",
                backend_url=f"http://127.0.0.1:{PORT}",
                backend_ready_source="http_server_started",
                ui_status="backend_ready",
                event="BACKEND_READY_PUBLISHED",
                message="Backend HTTP pronto.",
            )
            logger.info("BOOT_TIMELINE_START")
            logger.info("BACKEND_HTTP_READY url=http://127.0.0.1:%s", PORT)
            if KIRA_BOOT_MINIMAL:
                logger.info("BOOT_MINIMAL_BACKEND_READY")
            logger.info("[BACKEND_READY_PUBLISHED] backend_ready=true url=http://127.0.0.1:%s", PORT)
            boot_orchestrator.set_flag("backend_http_ok", True)
            boot_orchestrator.set_phase(BootPhase.BACKEND_READY, details=f"http://127.0.0.1:{PORT}")
            get_runtime_status_manager().set_backend_ready(True)
            _start_memory_boot_health_thread()

        server.startup = startup_with_event

        import asyncio
        asyncio.run(server.serve())
    except Exception as exc:
        _server_start_error = f"{type(exc).__name__}: {exc}"
        boot_orchestrator.fail(_server_start_error)
        logger.exception("[BACKEND] ERRO FATAL ao iniciar servidor HTTP")
        raise


def _check_port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", int(port))) != 0
    finally:
        sock.close()


def _install_backend_exception_hook() -> None:
    def _exception_handler(exc_type, exc_value, exc_traceback):
        logger.error("=" * 80)
        logger.error("EXCECAO NAO TRATADA DETECTADA NO BACKEND")
        logger.error("Tipo: %s", getattr(exc_type, "__name__", str(exc_type)))
        logger.error("Mensagem: %s", exc_value)
        logger.error("=" * 80)
        import traceback

        traceback.print_exception(exc_type, exc_value, exc_traceback)

    sys.excepthook = _exception_handler


def _configure_backend_file_logging() -> None:
    log_dir = BASE_DIR / "logs" / "Backend"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        from services.kira_log_manager import get_log_run_id

        run_id = get_log_run_id()
    except Exception:
        run_id = str(os.getenv("KIRA_LOG_RUN_ID", "")).strip() or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        os.environ.setdefault("KIRA_LOG_RUN_ID", run_id)
    log_path = log_dir / f"backend_{run_id}.log"
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                existing_path = Path(getattr(handler, "baseFilename", "")).resolve()
                if existing_path.parent == log_dir.resolve() and existing_path.name.startswith("backend_"):
                    return
            except Exception:
                pass
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    logger.info("LOG_RUN_ID_REUSED id=%s", run_id)
    logger.info("LOG_WRITER_UNIFIED component=backend_file_logger")
    logger.info("LOG_FILE_REUSED path=%s", log_path)
    logger.info("[BACKEND_LOG_PATH_SET] path=%s", log_path)
    for legacy_name in ("kira_backend.log", "kira_backend_output.log"):
        legacy_path = BASE_DIR / legacy_name
        if legacy_path.exists():
            try:
                legacy_path.unlink()
                logger.info("[ROOT_BACKEND_LOG_LEGACY_REMOVED] path=%s", legacy_path)
            except Exception as exc:
                logger.warning("[ROOT_BACKEND_LOG_LEGACY_REMOVE_FAILED] path=%s error=%s", legacy_path, exc)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  AGENTE (subprocess)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 
def _active_room_name_for_recovery() -> str:
    with _runtime_lock:
        return str(_runtime_readiness.room_name or _runtime_readiness.requested_room_name or "").strip()


def _recover_agent_process_after_failure(*, trigger_reason: str) -> bool:
    global _agent_proc, _agent_restart_count, _agent_last_restart_at
    room_name = _active_room_name_for_recovery()
    if not room_name:
        _runtime_log("SESSION_RECOVERY_SKIPPED", reason="no_active_room", trigger=trigger_reason)
        return False

    with _agent_recovery_lock:
        restart_delay = _agent_restart_policy.request()
        if restart_delay is None:
            _runtime_log("SESSION_RECOVERY_ABORTED", reason="restart_limit", attempts=_agent_restart_policy.attempts, room=room_name)
            _update_runtime_readiness(
                boot_recovery_active=False,
                ui_status="error",
                event="SESSION_RECOVERY_ABORTED",
                error="restart_limit",
                failure_reason="A Kira caiu varias vezes seguidas.",
            )
            return False
        _agent_restart_count += 1
        _agent_last_restart_at = time.time()
        logger.warning("AGENT_RESTART_BACKOFF delay_s=%.0f attempt=%s", restart_delay, _agent_restart_count)
        time.sleep(restart_delay)
        logger.info("SESSION_RECOVERY_AGENT_DEATH_DETECTED reason=%s room=%s", trigger_reason, room_name)
        logger.info(
            "SESSION_RECOVERY_SNAPSHOT_CREATED room=%s provider=%s ui_connected=%s functional_ready=%s reason=%s",
            room_name,
            provider_kind_from_snapshot(_runtime_state_snapshot()),
            str(bool(_runtime_state_snapshot().get("ui_room_connected") or _runtime_state_snapshot().get("ui_ready"))).lower(),
            str(bool(_runtime_state_snapshot().get("functional_ready"))).lower(),
            trigger_reason,
        )
        _runtime_log("SESSION_RECOVERY_START", attempt=_agent_restart_count, room=room_name, trigger=trigger_reason)
        _update_runtime_readiness(
            boot_recovery_active=True,
            ui_status="connecting",
            event="SESSION_RECOVERY_START",
            message="Recuperando sessao da Kira...",
            error="",
            failure_reason="",
        )

        recovery_credential = _current_livekit_credential_for_session()
        if recovery_credential is None:
            _runtime_log("SESSION_RECOVERY_FAILED", reason="missing_livekit_credential", room=room_name)
            return False

        worker_ready_url = _normalize_ready_probe_url(LIVEKIT_WORKER_HTTP_URL)
        worker_alive, worker_detail = _probe_livekit_worker_http_ready(worker_ready_url)
        if worker_alive:
            logger.info("SESSION_RECOVERY_WORKER_READY")
            _runtime_log(
                "SESSION_RECOVERY_REUSING_WORKER",
                room=room_name,
                url=worker_ready_url,
                detail=worker_detail or "worker_alive",
            )
        else:
            logger.info("SESSION_RECOVERY_AGENT_RESTART_BEGIN")
            _agent_proc = None
            start_agent()
            if _agent_proc is None:
                logger.error("SESSION_RECOVERY_FAILED reason=agent_restart_failed")
                return False
            logger.info("SESSION_RECOVERY_AGENT_RESTART_OK")

        try:
            logger.info("SESSION_RECOVERY_ROOM_REDISPATCH_BEGIN room=%s", room_name)
            asyncio.run(_ensure_agent_dispatch(room_name, recovery_credential))
            logger.info("SESSION_RECOVERY_ROOM_REDISPATCH_OK room=%s", room_name)
        except Exception as exc:
            _runtime_log("SESSION_RECOVERY_FAILED", reason=str(exc), room=room_name)
            logger.error("SESSION_RECOVERY_FAILED reason=%s", exc)
            _update_runtime_readiness(
                ui_status="error",
                event="SESSION_RECOVERY_FAILED",
                error=str(exc),
                failure_reason="Falha ao recuperar a sessao da Kira.",
                message="Nao foi possivel recuperar a sessao automaticamente.",
            )
            return False

        _runtime_log("SESSION_RECOVERY_DISPATCHED", attempt=_agent_restart_count, room=room_name)
        if not bool(_runtime_state_snapshot().get("ui_room_connected") or _runtime_state_snapshot().get("ui_ready")):
            logger.info("SESSION_RECOVERY_UI_RECONNECT_REQUIRED")
        else:
            logger.info("SESSION_RECOVERY_UI_RECONNECT_OK")
        _update_runtime_readiness(
            boot_recovery_active=True,
            ui_status="connecting",
            event="SESSION_RECOVERY_DISPATCHED",
            message="Reanexando agente a room atual...",
        )
        return True


def _terminate_agent_process_instance(proc, *, expected: bool = False) -> None:
    global _agent_proc
    if proc is None:
        return
    if expected:
        with _livekit_failed_slots_lock:
            _agent_expected_exit_pids.add(int(proc.pid))
    child_processes = []
    try:
        import psutil

        child_processes = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        child_processes = []
    try:
        if proc.poll() is None:
            logger.info(
                "SUBPROCESS_GRACEFUL_STOP_BY_PID pid=%s name=agent expected=%s",
                getattr(proc, "pid", None),
                str(expected).lower(),
            )
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=12)
    except Exception:
        try:
            logger.warning(
                "SUBPROCESS_KILL_ESCALATED pid=%s name=agent reason=timeout_or_terminate_failed",
                getattr(proc, "pid", None),
            )
            proc.kill()
        except Exception:
            pass
    for child in child_processes:
        try:
            if child.is_running():
                child.kill()
        except Exception:
            pass
    if _agent_proc is proc:
        _agent_proc = None


def _rotate_agent_livekit_slot_after_failure(
    *,
    watched_proc,
    failed_slot: int | None,
    status: str,
    error_text: str,
) -> None:
    with _agent_recovery_lock:
        if watched_proc is None or _agent_proc is not watched_proc:
            return

        _mark_livekit_failed_slot(failed_slot, reason=status)
        next_credential = _select_livekit_worker_credential()
        if next_credential is None:
            _runtime_log("LIVEKIT_ALL_SLOTS_FAILED", failed_slot=failed_slot, reason=status)
            _update_livekit_runtime_state(
                livekit_current_slot=failed_slot,
                livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                livekit_status="all_failed",
                voice_mode="unavailable",
                restart_required_for_voice=True,
                livekit_message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
                log_reason="worker_all_failed",
            )
            _update_runtime_readiness(
                ui_status="backend_ready",
                event="LIVEKIT_ALL_FAILED",
                error="",
                failure_reason="",
                message="LiveKit indisponivel. Nao foi possivel preparar o token da sessao.",
            )
            _publish_backend_ready_if_possible()
            _terminate_agent_process_instance(watched_proc, expected=True)
            return

        _update_runtime_readiness(
            ui_status="connecting",
            event="LIVEKIT_SLOT_RETRY",
            message="Credencial LiveKit falhou. Tentando o proximo slot...",
            error="",
            failure_reason="",
        )
        _terminate_agent_process_instance(watched_proc, expected=True)
        start_agent()


def _continue_livekit_rotation_after_worker_abort(
    *,
    watched_proc,
    exit_code: int,
) -> bool:
    if exit_code not in {LIVEKIT_WORKER_EXIT_AUTH_FAILED, LIVEKIT_WORKER_EXIT_QUOTA_FAILED}:
        return False

    failed_slot = LIVEKIT_MANAGER.current_slot
    if exit_code == LIVEKIT_WORKER_EXIT_AUTH_FAILED:
        _runtime_log("LIVEKIT_SLOT_AUTH_FAILED_CONTINUE", slot=failed_slot)
        _rotate_agent_livekit_slot_after_failure(
            watched_proc=watched_proc,
            failed_slot=failed_slot,
            status="auth_failed",
            error_text="worker_auth_failed_exit",
        )
        return True

    _runtime_log("LIVEKIT_SLOT_QUOTA_FAILED_CONTINUE", slot=failed_slot)
    _rotate_agent_livekit_slot_after_failure(
        watched_proc=watched_proc,
        failed_slot=failed_slot,
        status="quota_exceeded",
        error_text="worker_quota_failed_exit",
    )
    return True


def _set_agent_process_priority(pid: int) -> bool:
    """HOTFIX UI/CPU 3: aplica Above Normal ao agent.py no Windows, sem quebrar boot."""
    if sys.platform != "win32":
        logger.info("AGENT_PRIORITY_SKIPPED reason=not_windows pid=%s", pid)
        return False
    try:
        import psutil
    except ImportError:
        logger.warning("AGENT_PRIORITY_SKIPPED reason=psutil_unavailable pid=%s", pid)
        return False
    try:
        process = psutil.Process(int(pid))
        process.nice(psutil.HIGH_PRIORITY_CLASS)
        logger.info("AGENT_PRIORITY_SET pid=%s priority=high", pid)

        # 2026-11-01: o pid é o lançador do venv; o agente de verdade é o FILHO e ficava em prioridade normal. Num PC de
        # 4 núcleos cheio (VS Code, navegador, tela animada) a voz esperava na fila: travadas de 1-7 s no loop do agente.
        def children_too() -> None:
            for _ in range(6):
                time.sleep(5)
                try:
                    for child in process.children(recursive=True):
                        if child.nice() != psutil.HIGH_PRIORITY_CLASS:
                            child.nice(psutil.HIGH_PRIORITY_CLASS)
                            logger.info("AGENT_PRIORITY_SET pid=%s priority=high child_of=%s", child.pid, pid)
                except psutil.Error:
                    return

        threading.Thread(target=children_too, name="kira-agent-priority", daemon=True).start()
        return True
    except psutil.NoSuchProcess:
        logger.warning("AGENT_PRIORITY_FAILED reason=no_such_process pid=%s", pid)
    except psutil.AccessDenied:
        logger.warning("AGENT_PRIORITY_FAILED reason=access_denied pid=%s", pid)
    except Exception as exc:
        logger.warning("AGENT_PRIORITY_FAILED reason=%s pid=%s", type(exc).__name__, pid)
    return False


def start_agent():
    global _agent_proc
    try:
        logger.info("[AGENT_START] requested")
        _runtime_log("AGENT_START_REQUESTED")
        boot_orchestrator.set_phase(BootPhase.AGENT_STARTING, details="start_agent")
        try:
            _kill_port_8081_if_occupied()
        except RuntimeError as exc:
            _update_runtime_readiness(
                agent_process_spawned=False,
                agent_connected=False,
                agent_ready=False,
                backend_ready=False,
                ui_status="error",
                event="PORT_8081_BLOCKED",
                message=str(exc),
                error=str(exc),
                failure_reason=str(exc),
            )
            boot_orchestrator.fail(str(exc))
            return
        if not ensure_livekit_server_ready():
            _update_livekit_runtime_state(
                livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                livekit_status="error",
                voice_mode="unavailable",
                restart_required_for_voice=True,
                livekit_message="LiveKit local nao respondeu em localhost:7880.",
                log_reason="worker_boot_preflight_failed",
            )
            _update_runtime_readiness(
                agent_process_spawned=False,
                agent_connected=False,
                agent_ready=False,
                backend_ready=False,
                ui_status="error",
                event="LIVEKIT_UNAVAILABLE",
                message="LiveKit local indisponivel.",
                error="LiveKit local nao respondeu em localhost:7880",
                failure_reason="LiveKit local indisponivel.",
            )
            boot_orchestrator.fail("LiveKit local indisponivel para iniciar o worker.")
            return

        credential = _select_bootstrap_livekit_credential()
        if credential is None:
            boot_orchestrator.fail("Nenhuma credencial LiveKit disponivel para bootstrap do worker.")
            return

        _runtime_log("AGENT_START", cwd=AGENT_DIR, cmd=" ".join(AGENT_CMD))
        _agent_env = os.environ.copy()
        _agent_env["KIRA_LAUNCHED_BY_APP"] = "1"
        _agent_env["PYTHONIOENCODING"] = "utf-8"
        _agent_env["KIRA_INTERNAL_TOKEN"] = KIRA_INTERNAL_TOKEN
        _apply_livekit_credential_to_env(_agent_env, credential)
        _update_livekit_runtime_state(
            livekit_current_slot=credential.slot,
            livekit_total_slots=LIVEKIT_MANAGER.total_slots,
            livekit_status="ready",
            livekit_worker_boot_status="starting",
            livekit_ready_source="worker_boot",
            voice_mode="voice",
            restart_required_for_voice=False,
            livekit_message=_livekit_runtime_message_for_status("worker_booting"),
            log_reason="worker_boot",
        )
        logger.info("[LIVEKIT_WORKER_BOOT_START] livekit_status=ready worker_boot_status=starting")
        existing_pythonpath = [
            entry for entry in str(_agent_env.get("PYTHONPATH", "") or "").split(os.pathsep) if entry
        ]
        pythonpath_entries: list[str] = []
        for candidate in (str(BASE_DIR), str(AGENT_DIR), *existing_pythonpath):
            if candidate and candidate not in pythonpath_entries:
                pythonpath_entries.append(candidate)
        _agent_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        _agent_proc = tracker.start_and_register(
            name="agent_main",
            args=AGENT_CMD,
            cwd=AGENT_DIR,
            env=_agent_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                          if sys.platform == "win32" else 0,
        )
        print(f"[KIRA] Agente iniciado (PID {_agent_proc.pid})")
        logger.info("[AGENT_START] process_spawned pid=%s", _agent_proc.pid)
        logger.info("[AGENT_PROCESS_SPAWNED] pid=%s", _agent_proc.pid)
        logger.info("KIRA_AGENT_PROCESS_SINGLETON_OK pid=%s python=%s", _agent_proc.pid, AGENT_CMD[0])
        _set_agent_process_priority(_agent_proc.pid)
        boot_orchestrator.set_flag("agent_process_spawned", True)
        _runtime_log("AGENT_PROCESS_SPAWNED", pid=_agent_proc.pid)
        _update_runtime_readiness(
            agent_process_spawned=True,
            agent_process_dead=False,
            agent_error=False,
            agent_status="starting",
            agent_heartbeat_ok=False,
            agent_heartbeat_status="pending",
            agent_heartbeat_stale_since=0.0,
            last_agent_heartbeat_at=0.0,
            event="AGENT_PROCESS_SPAWNED",
            message="Processo do agente iniciado.",
            ui_status="connecting",
            error="",
            failure_reason="",
        )
        livekit_abort_reason: str | None = None
        livekit_abort_detail: str = ""
        worker_ready_state = {"seen": False}
        worker_ready_lock = threading.Lock()
        recent_agent_output = deque(maxlen=80)

        def _mark_worker_ready(*, source: str, detail: str = "") -> bool:
            with worker_ready_lock:
                if worker_ready_state["seen"]:
                    return False
                worker_ready_state["seen"] = True

            LIVEKIT_MANAGER.save_last_good_slot(credential.slot)
            _clear_livekit_failed_slots()
            _update_livekit_runtime_state(livekit_current_slot=credential.slot, livekit_total_slots=LIVEKIT_MANAGER.total_slots)
            _runtime_log("LIVEKIT_SLOT_OK", slot=credential.slot, reason=source)
            _runtime_log("AGENT_CONNECTED", detail=detail or source)
            snapshot = mark_agent_worker_ready(
                source="backend_probe_8081" if source == "http_probe" else source,
                phase="worker_http_ready" if source == "http_probe" else "worker_ready",
                pid=getattr(_agent_proc, "pid", None),
            )
            logger.info(
                "[KIRA READY STATE] worker_ready_applied source=%s pre_start_ready=%s backend_ready=%s livekit_status=%s",
                source,
                str(bool(snapshot.get("pre_start_ready"))).lower(),
                str(bool(snapshot.get("backend_ready"))).lower(),
                snapshot.get("livekit_status"),
            )
            logger.info("[AGENT_START] worker_ready source=%s detail=%s", source, detail or source)
            logger.info("[LIVEKIT_READY_CONFIRMED_AFTER_WORKER_READY] livekit_status=ready")
            _runtime_log("AGENT_READY", source=source)
            return True

        def _watch_stdout():
            nonlocal livekit_abort_reason, livekit_abort_detail
            livekit_rotation_scheduled = False
            for line in _agent_proc.stdout:
                _write_stdout_watcher_line_safe(line)
                raw = str(line or "").strip()
                if raw:
                    recent_agent_output.append(raw)
                low = raw.lower()
                if "boot em modo" in low or "starting worker" in low:
                    _runtime_log("AGENT_CONNECTING", detail=raw[:160])
                    _update_runtime_readiness(
                        agent_connected=True,
                        event="AGENT_CONNECTING",
                        message="Agente conectando ao runtime.",
                        ui_status="connecting",
                    )
                elif (
                    "worker pronto" in low
                    or "pronto para conexoes" in low
                    or "worker registered" in low
                    or "livekit_worker_registered" in low
                    or "registered worker" in low
                    or "ready to accept jobs" in low
                ):
                    _mark_worker_ready(source="stdout", detail=raw[:160])
                elif (
                    not worker_ready_state["seen"]
                    and not livekit_rotation_scheduled
                    and "[ui_notify]" not in low
                    and "[info]" not in low
                    and is_livekit_auth_error(raw)
                ):
                    livekit_rotation_scheduled = True
                    livekit_abort_reason = "auth_failed"
                    livekit_abort_detail = raw
                    logger.warning("LIVEKIT_AUTH_FAILED slot=%s error=%s", credential.slot, raw)
                    _runtime_log("LIVEKIT_AUTH_FAILED", slot=credential.slot)
                    _update_livekit_runtime_state(
                        livekit_current_slot=credential.slot,
                        livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                        livekit_status="auth_failed",
                        voice_mode="voice",
                        restart_required_for_voice=False,
                        livekit_message=_livekit_runtime_message_for_status("auth_failed"),
                        log_reason="worker_auth_failed",
                    )
                elif (
                    not worker_ready_state["seen"]
                    and not livekit_rotation_scheduled
                    and "[ui_notify]" not in low
                    and "[info]" not in low
                    and is_livekit_quota_error(raw)
                ):
                    livekit_rotation_scheduled = True
                    livekit_abort_reason = "quota_exceeded"
                    livekit_abort_detail = raw
                    logger.warning("LIVEKIT_QUOTA_EXCEEDED_ON_STARTUP slot=%s error=%s", credential.slot, raw)
                    _runtime_log("LIVEKIT_QUOTA_EXCEEDED_ON_STARTUP", slot=credential.slot)
                    _update_livekit_runtime_state(
                        livekit_current_slot=credential.slot,
                        livekit_total_slots=LIVEKIT_MANAGER.total_slots,
                        livekit_status="quota_exceeded",
                        voice_mode="voice",
                        restart_required_for_voice=False,
                        livekit_message="LiveKit sem cota neste slot. Tentando outra credencial LiveKit.",
                        log_reason="worker_quota_failed",
                    )
                if "session_room_connected" in low or "depois de session.start" in low:
                    if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                        logger.info("AGENT_LOCAL_ROOM_CONNECT_OK room=%s", _runtime_readiness.room_name or _runtime_readiness.requested_room_name or "")
                    _update_runtime_readiness(
                        session_started=True,
                        room_ready=True,
                        room_connected=True,
                        room_connecting=False,
                        boot_state="room_ready",
                        ui_status="connecting",
                        event="SESSION_ROOM_CONNECTED",
                        message="Room conectada.",
                    )
                    _runtime_log("SESSION_ROOM_CONNECTED")
                    _runtime_log(
                        "WAITING_AGENT_RUNTIME_READY",
                        room=_runtime_readiness.room_name or _runtime_readiness.requested_room_name or None,
                    )
                elif "aguardando gemini live ficar pronto" in low:
                    if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                        logger.info("AGENT_LOCAL_GEMINI_BEGIN")
                    _update_runtime_readiness(
                        gemini_connecting=True,
                        boot_state="gemini_connecting",
                        ui_status="connecting",
                        event="BOOT_RECOVERY_START" if _runtime_readiness.boot_recovery_active else "GEMINI_CONNECTING",
                        message="Aguardando Gemini Live...",
                    )
                    if _runtime_readiness.boot_recovery_active:
                        _runtime_log("BOOT_RECOVERY_START")
                elif "gemini_ready" in low:
                    if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                        logger.info("AGENT_LOCAL_GEMINI_READY")
                    _update_runtime_readiness(
                        gemini_ready=True,
                        gemini_connecting=False,
                        boot_state="gemini_ready",
                        ui_status="connecting",
                        event="GEMINI_READY",
                        message="Gemini pronto.",
                    )
                    _runtime_log("GEMINI_READY")
                elif "voice_pipeline_ready" in low:
                    if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                        logger.info("AGENT_LOCAL_VOICE_PIPELINE_BEGIN")
                        logger.info("AGENT_LOCAL_VOICE_PIPELINE_READY")
                    _update_runtime_readiness(
                        gemini_ready=True,
                        gemini_connecting=False,
                        voice_pipeline_ready=True,
                        boot_state="gemini_ready",
                        ui_status="connecting",
                        event="VOICE_PIPELINE_READY",
                        message="Pipeline de voz pronto.",
                    )
                    _runtime_log("VOICE_PIPELINE_READY")
                elif "session_runtime_ready" in low or "handlers de sala registrados" in low:
                    _update_runtime_readiness(
                        handlers_registered=True,
                        runtime_ready=True,
                        boot_recovery_active=False,
                        boot_state="runtime_ready",
                        ui_status="connecting",
                        event="SESSION_RUNTIME_READY",
                        message="Handlers e runtime prontos.",
                    )
                    _runtime_log("SESSION_RUNTIME_READY")
                    _publish_backend_ready_if_possible()
                elif "runtime da sala pronto" in low:
                    _update_runtime_readiness(
                        runtime_ready=True,
                        boot_recovery_active=False,
                        boot_state="runtime_ready",
                        ui_status="connecting",
                        event="SESSION_RUNTIME_READY",
                    )
                    _runtime_log("SESSION_RUNTIME_READY")
                    _publish_backend_ready_if_possible()
                elif "startup_greeting_created" in low:
                    _update_runtime_readiness(
                        startup_greeting_created=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_CREATED",
                        message="Saudacao inicial criada.",
                    )
                    _runtime_log("STARTUP_GREETING_CREATED")
                elif "startup_greeting_sanitized" in low:
                    _update_runtime_readiness(
                        startup_greeting_sanitized=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_SANITIZED",
                        message="Saudacao inicial validada.",
                    )
                    _runtime_log("STARTUP_GREETING_SANITIZED")
                elif "startup_greeting_enqueued" in low:
                    _update_runtime_readiness(
                        startup_greeting_enqueued=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_ENQUEUED",
                        message="Saudacao inicial enfileirada.",
                    )
                    _runtime_log("STARTUP_GREETING_ENQUEUED")
                elif "startup_greeting_dispatched" in low:
                    _update_runtime_readiness(
                        startup_greeting_dispatched=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_DISPATCHED",
                        message="Saudacao inicial enviada ao pipeline de voz.",
                    )
                    _runtime_log("STARTUP_GREETING_DISPATCHED")
                elif "startup_greeting_speak_start" in low:
                    _update_runtime_readiness(
                        startup_greeting_speaking=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_SPEAK_START",
                        message="Kira iniciou a saudacao inicial.",
                    )
                    _runtime_log("STARTUP_GREETING_SPEAK_START")
                elif "startup_greeting_speak_done" in low:
                    _update_runtime_readiness(
                        startup_greeting_speaking=False,
                        startup_greeting_done=True,
                        ui_status="connecting",
                        event="STARTUP_GREETING_SPEAK_DONE",
                        message="Saudacao inicial concluida.",
                    )
                    _runtime_log("STARTUP_GREETING_SPEAK_DONE")
                    _publish_functional_ready_if_possible()
                elif "functional_ready_failed" in low:
                    _update_runtime_readiness(
                        startup_greeting_speaking=False,
                        functional_ready=False,
                        ui_status="connecting",
                        event="FUNCTIONAL_READY_FAILED",
                        message="Falha ao concluir a primeira prontidao funcional.",
                    )
                    _runtime_log("FUNCTIONAL_READY_FAILED")
                elif "functional_ready" in low:
                    if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                        logger.info("AGENT_LOCAL_FUNCTIONAL_READY")
                    _update_runtime_readiness(
                        startup_greeting_speaking=False,
                        startup_greeting_done=True,
                        functional_ready=True,
                        boot_recovery_active=False,
                        ui_status="connecting",
                        event="FUNCTIONAL_READY",
                        message="Saudacao inicial concluida.",
                    )
                    _runtime_log("FUNCTIONAL_READY")
                    _publish_functional_ready_if_possible()
                elif "[lembrete] lembrete agendado" in low:
                    _update_runtime_readiness(event="REMINDER_SCHEDULED", message="Lembrete agendado.")
                    _runtime_log("UI_RUNTIME_EVENT_PUBLISHED", event="REMINDER_SCHEDULED")
                elif "[lembrete] disparando lembrete tentativa" in low:
                    _update_runtime_readiness(event="REMINDER_DELIVERY_STARTED", message="Entregando lembrete.")
                    _runtime_log("UI_RUNTIME_EVENT_PUBLISHED", event="REMINDER_DELIVERY_STARTED")
                elif "[lembrete] lembrete entregue na tentativa" in low:
                    _update_runtime_readiness(event="REMINDER_DELIVERED", message="Lembrete entregue.")
                    _runtime_log("UI_REMINDER_DELIVERED")
                elif "livekit voice: agent_state" in low:
                    if "-> speaking" in low:
                        _update_runtime_readiness(agent_state="speaking")
                    elif "-> thinking" in low:
                        _update_runtime_readiness(agent_state="thinking")
                    elif "-> listening" in low:
                        _update_runtime_readiness(agent_state="listening")
                    elif "-> idle" in low:
                        _update_runtime_readiness(agent_state="idle")
                elif "time out em session.start" in low or "timeout aguardando gemini live pronto" in low:
                    _update_runtime_readiness(
                        boot_recovery_active=True,
                        ui_status="connecting",
                        event="BOOT_RECOVERY_START",
                        message="Recuperando inicializacao...",
                    )
                    _runtime_log("BOOT_RECOVERY_START")
                elif "session_recovery_requested" in low or "session_recovery_start" in low:
                    _update_runtime_readiness(
                        boot_recovery_active=True,
                        ui_status="connecting",
                        event="SESSION_RECOVERY_START",
                        message="Recuperando sessao da Kira...",
                        error="",
                        failure_reason="",
                    )
                    _runtime_log("SESSION_RECOVERY_START")
                elif "session_recovery_dispatched" in low or "session_recovery_executing" in low:
                    _update_runtime_readiness(
                        boot_recovery_active=True,
                        ui_status="connecting",
                        event="SESSION_RECOVERY_DISPATCHED",
                        message="Reanexando agente a room atual...",
                        error="",
                        failure_reason="",
                    )
                    _runtime_log("SESSION_RECOVERY_DISPATCHED")
                elif "reconexao preventiva concluida com sucesso" in low:
                    _update_runtime_readiness(boot_recovery_active=False, event="BOOT_RECOVERY_DONE")
                    _runtime_log("BOOT_RECOVERY_DONE")
                _publish_backend_ready_if_possible()
                _publish_functional_ready_if_possible()

        def _probe_worker_http_ready():
            watched_proc = _agent_proc
            if watched_proc is None:
                return
            probe_url = _normalize_ready_probe_url(LIVEKIT_WORKER_HTTP_URL)
            logger.info("[KIRA READY PROBE] start url=%s", probe_url)
            deadline = time.monotonic() + WORKER_HTTP_READY_TIMEOUT_SECONDS
            last_probe_detail = ""
            retry_interval = 1.0
            probe_started_at = time.monotonic()
            while time.monotonic() < deadline:
                if _agent_ready.is_set():
                    return
                if _agent_proc is not watched_proc or watched_proc.poll() is not None:
                    return
                ready, probe_detail = _probe_livekit_worker_http_ready(probe_url, expected_proc=watched_proc)
                if ready:
                    logger.info("WORKER_HTTP_PROBE_OK url=%s %s", probe_url, probe_detail)
                    logger.info("[KIRA READY PROBE] ok url=%s %s", probe_url, probe_detail)
                    logger.info("[WORKER_HTTP_READY] elapsed=%.1fs", time.monotonic() - probe_started_at)
                    _runtime_log("WORKER_HTTP_READY", url=probe_url, detail=probe_detail)
                    _mark_worker_ready(source="http_probe", detail=probe_detail)
                    return
                if probe_detail != last_probe_detail:
                    logger.info("[KIRA READY PROBE] not_ready url=%s reason=%s", probe_url, probe_detail)
                    last_probe_detail = probe_detail
                logger.info("[WORKER_HTTP_PROBE_RETRY] waiting=%.1fs", retry_interval)
                time.sleep(retry_interval)
                retry_interval = min(retry_interval * 1.5, 5.0)
            logger.warning("[KIRA READY PROBE] not_ready url=%s reason=timeout_waiting_worker_http", probe_url)
            logger.error("[WORKER_HTTP_TIMEOUT] max_wait=%.1fs", WORKER_HTTP_READY_TIMEOUT_SECONDS)
            if watched_proc.poll() is None:
                logger.warning("[AGENT_HEALTH_DEGRADED] probe_failed=true process_alive=true")
                logger.info("[AGENT_FLAGS_NOT_RESET] reason=process_alive_probe_failed")

        def _watch_agent_exit():
            watched_proc = _agent_proc
            if watched_proc is None:
                return
            exit_code = watched_proc.wait()
            if provider_kind_from_snapshot(_runtime_state_snapshot()) == "local":
                logger.info("AGENT_LOCAL_EXIT code=%s stage=%s", exit_code, _runtime_state_snapshot().get("event") or "")
            if _consume_expected_agent_exit(int(watched_proc.pid)):
                logger.info("AGENT_PROCESS_EXIT_EXPECTED pid=%s exit_code=%s", watched_proc.pid, exit_code)
                return
            mark_agent_process_dead(exit_code=exit_code, reason=f"agent_exit:{exit_code}")
            logger.error("[AGENT_PROCESS_DIED] exit_code=%s", exit_code)
            logger.warning("[AGENT_PROCESS_CONFIRMED_DEAD] returncode=%s", exit_code)
            if recent_agent_output:
                logger.error("[AGENT_STDOUT_FINAL] %s", "\n".join(list(recent_agent_output)[-25:])[-1000:])
            _agent_ready.clear()
            if livekit_abort_reason in {"auth_failed", "quota_exceeded"} and not worker_ready_state["seen"]:
                _rotate_agent_livekit_slot_after_failure(
                    watched_proc=watched_proc,
                    failed_slot=credential.slot,
                    status=str(livekit_abort_reason),
                    error_text=livekit_abort_detail or f"worker_{livekit_abort_reason}_stdout",
                )
                return
            if _continue_livekit_rotation_after_worker_abort(
                watched_proc=watched_proc,
                exit_code=exit_code,
            ):
                return
            if exit_code == 0:
                return
            recent_tail = "\n".join(list(recent_agent_output)[-25:])
            last_error_line = ""
            for line in reversed(recent_tail.splitlines()):
                clean_line = line.strip()
                if clean_line and (
                    "Traceback" in clean_line
                    or "Error:" in clean_line
                    or "Exception" in clean_line
                    or "ModuleNotFoundError" in clean_line
                    or "ImportError" in clean_line
                ):
                    last_error_line = clean_line[-500:]
                    break
            if not last_error_line:
                last_error_line = f"Agente encerrou com codigo {exit_code}"
            logger.error("[AGENT_BOOT_FAILED_VISIBLE] error=%s", last_error_line)
            _runtime_log("AGENT_FAILED", exit_code=exit_code)
            active_switch = _get_active_transport_switch()
            if active_switch and str(active_switch.get("target_provider") or "") == "local" and not bool(_runtime_state_snapshot().get("functional_ready")):
                _update_active_transport_switch(
                    str(active_switch.get("switch_id") or ""),
                    phase="failed",
                    failed=True,
                    failure_reason="agent_process_died_before_functional_ready",
                )
                logger.warning(
                    "ECONOMY_TRANSPORT_SWITCH_LATE_FAILURE_DETECTED switch_id=%s provider=local reason=agent_process_died_before_functional_ready",
                    active_switch.get("switch_id") or "",
                )
                return
            if exit_code == 1:
                logger.info("AGENT_RESTART_REQUESTED reason=process_died_exit_1")
            if _recover_agent_process_after_failure(trigger_reason=f"agent_exit:{exit_code}"):
                logger.info("AGENT_RESTART_OK")
                return
            logger.error("AGENT_RESTART_FAILED")
            _update_runtime_readiness(
                agent_process_spawned=True,
                agent_process_dead=True,
                agent_connected=False,
                agent_ready=False,
                agent_error=True,
                agent_heartbeat_ok=False,
                agent_status="error",
                agent_last_error=last_error_line,
                ui_status="error",
                event="AGENT_FAILED",
                error=last_error_line,
                failure_reason="Processo do agente encerrou inesperadamente.",
            )

        threading.Thread(target=_watch_stdout, daemon=True).start()
        threading.Thread(target=_probe_worker_http_ready, daemon=True).start()
        threading.Thread(target=_watch_agent_exit, daemon=True).start()

        def _warn_agent_bootstrap_waiting():
            time.sleep(8.0)
            if _agent_ready.is_set():
                return
            if _agent_proc and _agent_proc.poll() is None:
                _runtime_log("AGENT_WAITING_WORKER_READY", detail="process_alive_but_not_registered", worker_ready="false")
                logger.info("[AGENT_WAITING_WORKER_READY] detail=process_alive_but_not_registered worker_ready=false")
                _update_runtime_readiness(
                    agent_process_spawned=True,
                    agent_process_alive=True,
                    agent_worker_ready=False,
                    worker_ready_received=False,
                    agent_bootstrap_status="waiting_worker_ready",
                    agent_ready=False,
                    backend_ready=False,
                    event="AGENT_WAITING_WORKER_READY",
                    message="Aguardando worker registrar no LiveKit...",
                    ui_status="connecting",
                )

        threading.Thread(target=_warn_agent_bootstrap_waiting, daemon=True).start()

    except Exception as e:
        logger.exception("[AGENT_START] falha ao iniciar agente")
        print(f"[KIRA] Erro ao iniciar agente: {e}", flush=True)
        _runtime_log("AGENT_START_FAILED", reason=str(e))
        _runtime_log("AGENT_FAILED", reason=str(e))
        _update_runtime_readiness(
            agent_process_spawned=False,
            agent_connected=False,
            agent_ready=False,
            ui_status="error",
            event="AGENT_START_FAILED",
            error=str(e),
            failure_reason=str(e),
        )


def stop_agent():
    global _agent_proc
    _reset_ready_republish_guard("stop_agent")
    child_processes = []
    if _agent_proc:
        with _livekit_failed_slots_lock:
            _agent_expected_exit_pids.add(int(_agent_proc.pid))
    if _agent_proc:
        try:
            import psutil

            child_processes = psutil.Process(_agent_proc.pid).children(recursive=True)
        except Exception:
            child_processes = []
    if _agent_proc and _agent_proc.poll() is None:
        try:
            logger.info("SUBPROCESS_GRACEFUL_STOP_BY_PID pid=%s name=agent", getattr(_agent_proc, "pid", None))
            if sys.platform == "win32":
                _agent_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _agent_proc.terminate()
            _agent_proc.wait(timeout=12)
        except Exception:
            logger.warning(
                "SUBPROCESS_KILL_ESCALATED pid=%s name=agent reason=timeout_or_terminate_failed",
                getattr(_agent_proc, "pid", None),
            )
            _agent_proc.kill()
        for child in child_processes:
            try:
                if child.is_running():
                    child.kill()
            except Exception:
                pass
        print("[KIRA] Agente encerrado.")
    _agent_proc = None
    logger.info("[AGENT_FLAGS_RESET] reason=stop_agent")
    _update_runtime_readiness(
        ui_status="disconnected",
        agent_process_spawned=False,
        agent_connected=False,
        agent_ready=False,
        room_connecting=False,
        room_connected=False,
        room_ready=False,
        gemini_connecting=False,
        gemini_ready=False,
        session_started=False,
        handlers_registered=False,
        voice_pipeline_ready=False,
        runtime_ready=False,
        ui_ready=False,
        backend_ready=False,
        startup_greeting_created=False,
        startup_greeting_sanitized=False,
        startup_greeting_enqueued=False,
        startup_greeting_dispatched=False,
        startup_greeting_speaking=False,
        startup_greeting_done=False,
        functional_ready=False,
        boot_recovery_active=False,
        event="SHUTDOWN_DONE",
        message="Agente encerrado.",
        agent_state="idle",
        user_state="idle",
    )
    _agent_ready.clear()


_AGENT_RUNTIME_ALIVE_AT = [0.0]  # último heartbeat stage=runtime_alive (sessão rodando)


def _boot_timeout_watcher() -> None:
    while True:
        time.sleep(1.0)
        try:
            snapshot = _runtime_state_snapshot()
            last_hb = float(snapshot.get("last_agent_heartbeat_at") or 0.0)
            if last_hb > 0:
                age_ms = int(max((time.time() - last_hb) * 1000, 0))
                if age_ms > 15000 and bool(snapshot.get("agent_heartbeat_ok")):
                    state = mark_agent_heartbeat_stale(age_ms)
                    _schedule_transcription_status_push(state)
        except Exception as exc:
            logger.debug("AGENT_HEARTBEAT_STALE_CHECK_DROPPED error=%s", exc)
        with _runtime_lock:
            if _runtime_readiness.ui_ready or _runtime_readiness.failure_reason:
                continue
            elapsed = max(time.monotonic() - _runtime_readiness.boot_started_at, 0.0)
            room_requested = bool(str(_runtime_readiness.requested_room_name or "").strip())
            if room_requested and (not _runtime_readiness.room_ready) and elapsed >= BOOT_ROOM_TIMEOUT_SECONDS:
                _runtime_log("BOOT_TIMEOUT", stage="room", elapsed_s=f"{elapsed:.1f}")
                session_debug = _session_debug_snapshot()
                token_issued = bool(session_debug.get("token_issued"))
                active_session = bool(
                    getattr(_runtime_readiness, "active_session", False)
                    or _runtime_readiness.session_started
                    or token_issued
                )
                ui_room_connected = bool(session_debug.get("ui_room_connected"))
                worker_ready = bool(
                    session_debug.get("agent_worker_ready")
                    or _runtime_readiness.agent_ready
                    or _runtime_readiness.agent_connected
                )
                room_name = str(_runtime_readiness.room_name or _runtime_readiness.requested_room_name or "").strip()
                # Agente já conversando (pronto + sinal de vida recente): a sala está boa, só a marca room_ready não
                # chegou. Antes isso derrubava uma sessão funcionando (2026-11-07 22:35: "reconectando a Kira").
                if time.time() - _AGENT_RUNTIME_ALIVE_AT[0] < 15:
                    _runtime_readiness.room_ready = True
                    logger.info("SESSION_ROOM_TIMEOUT_SKIPPED_AGENT_ALIVE room=%s", room_name)
                    continue
                if active_session and token_issued and ui_room_connected and worker_ready and room_name:
                    logger.warning("[SESSION_ROOM_TIMEOUT_DETECTED] room=%s", room_name)
                    _debug_snap = _session_debug_snapshot()
                    _dispatch_info = _debug_snap.get("dispatch_info") or {}
                    _existing_dispatch_id = str(_dispatch_info.get("id") or "").strip() if isinstance(_dispatch_info, dict) else ""
                    _existing_dispatch = bool(_existing_dispatch_id)
                    _room_connected_now = bool(_runtime_readiness.room_ready)
                    logger.warning(
                        "SESSION_AGENT_JOIN_DIAG room=%s dispatch_id=%s entrypoint_state=%s current_job_age_s=%s",
                        room_name,
                        _existing_dispatch_id or "none",
                        "room_connected" if _room_connected_now else "before_room_connect",
                        "unknown",
                    )
                    if _existing_dispatch and not _room_connected_now:
                        # Dispatch criado mas agent nunca entrou: job provavelmente nao foi entregue
                        # pelo LiveKit server ao worker (race condition WS_FULL/WS_AVAILABLE).
                        # Deletar o dispatch travado e criar um novo para forcar nova tentativa.
                        logger.warning(
                            "SESSION_ROOM_TIMEOUT_EXISTING_DISPATCH_WAITING room=%s dispatch_active=true action=refresh",
                            room_name,
                        )
                        logger.warning(
                            "SESSION_AGENT_JOIN_BLOCKED_BY_STALE_ENTRYPOINT room=%s",
                            room_name,
                        )
                        logger.warning(
                            "SESSION_AGENT_JOIN_RECOVERY_REQUESTED reason=stale_entrypoint"
                        )
                        logger.info(
                            "SESSION_AGENT_JOIN_WAIT_START room=%s dispatch_id=%s",
                            room_name, _existing_dispatch_id or "none",
                        )
                        if _existing_dispatch_id:
                            try:
                                _credential = _resolve_livekit_credential_for_room(room_name)
                                asyncio.run(
                                    _delete_and_refresh_dispatch(
                                        room_name,
                                        _existing_dispatch_id,
                                        _credential,
                                        max_attempts=1,
                                    )
                                )
                            except Exception as _refresh_exc:
                                logger.warning(
                                    "SESSION_AGENT_JOIN_RETRY_FAILED room=%s error=%s",
                                    room_name, _refresh_exc,
                                )
                            refresh_deadline = time.monotonic() + 15.0
                            while time.monotonic() < refresh_deadline:
                                if _runtime_readiness.room_ready:
                                    logger.info("[SESSION_ROOM_TIMEOUT_RECOVERY_AFTER_RETRY] room=%s", room_name)
                                    break
                                time.sleep(0.4)
                            if _runtime_readiness.room_ready:
                                continue
                            logger.warning(
                                "SESSION_AGENT_JOIN_FAILED reason=entrypoint_stuck_before_room_connect"
                            )
                        else:
                            logger.warning(
                                "SESSION_ROOM_TIMEOUT_EXISTING_DISPATCH_NO_ID room=%s action=wait",
                                room_name,
                            )
                    elif _existing_dispatch and _room_connected_now:
                        logger.info("SESSION_ROOM_TIMEOUT_ALREADY_CONNECTED room=%s action=skip", room_name)
                        continue
                    else:
                        logger.warning("[SESSION_ROOM_TIMEOUT_DISPATCH_RETRY] room=%s", room_name)
                        try:
                            asyncio.run(_ensure_agent_dispatch(room_name, _resolve_livekit_credential_for_room(room_name)))
                            logger.info("[SESSION_ROOM_TIMEOUT_DISPATCH_RETRY_OK] room=%s", room_name)
                        except Exception as dispatch_retry_exc:
                            logger.warning("[SESSION_ROOM_TIMEOUT_DISPATCH_RETRY_FAILED] error=%s", dispatch_retry_exc)
                        retry_deadline = time.monotonic() + 6.0
                        while time.monotonic() < retry_deadline:
                            if _runtime_readiness.room_ready:
                                logger.info("[SESSION_ROOM_TIMEOUT_RECOVERY_AFTER_RETRY] room=%s", room_name)
                                break
                            time.sleep(0.4)
                        if _runtime_readiness.room_ready:
                            continue
                    logger.warning("[ROOM_WAITING_AFTER_DISPATCH_RETRY_FAILED] room=%s", room_name)
                _update_runtime_readiness(
                    boot_state="booting",
                    ui_status="connecting",
                    boot_recovery_active=True,
                    event="ROOM_WAITING",
                    message="Aguardando conexao da room...",
                    error="",
                    failure_reason="",
                    boot_started_at=time.monotonic(),
                )
                continue
            if room_requested and _runtime_readiness.room_ready and (not _runtime_readiness.gemini_ready) and elapsed >= BOOT_GEMINI_TIMEOUT_SECONDS:
                _runtime_log("BOOT_TIMEOUT", stage="gemini", elapsed_s=f"{elapsed:.1f}")
                _update_runtime_readiness(boot_recovery_active=True, event="BOOT_RECOVERY_START")
                continue
            if room_requested and _runtime_readiness.gemini_ready and (not _runtime_readiness.runtime_ready) and elapsed >= BOOT_RUNTIME_TIMEOUT_SECONDS:
                _runtime_log("BOOT_TIMEOUT", stage="runtime", elapsed_s=f"{elapsed:.1f}")
                runtime_timeout_message = "Room conectada, mas agente nao entrou/runtime nao iniciou."
                _runtime_log(
                    "RUNTIME_READY_TIMEOUT",
                    reason="room_connected_but_runtime_not_ready",
                    room=_runtime_readiness.room_name or _runtime_readiness.requested_room_name or None,
                    ui_room_connected="true" if bool(_session_debug_snapshot().get("ui_room_connected")) else "false",
                )
                _update_session_debug(
                    last_event="RUNTIME_READY_TIMEOUT",
                    last_message=runtime_timeout_message,
                    last_error=runtime_timeout_message,
                    last_stage="runtime_timeout",
                )
                _update_runtime_readiness(
                    boot_state="booting",
                    ui_status="connecting",
                    boot_recovery_active=True,
                    event="RUNTIME_WAITING",
                    message=runtime_timeout_message,
                    error="",
                    failure_reason="",
                    boot_started_at=time.monotonic(),
                )
                continue
            if room_requested and elapsed >= BOOT_TOTAL_TIMEOUT_SECONDS:
                _runtime_log("BOOT_TIMEOUT", stage="total", elapsed_s=f"{elapsed:.1f}")
                _update_runtime_readiness(
                    boot_state="booting",
                    ui_status="connecting",
                    boot_recovery_active=True,
                    event="BOOT_WAITING",
                    message="Inicializacao em andamento...",
                    error="",
                    failure_reason="",
                    boot_started_at=time.monotonic(),
                )


def _free_whatsapp_bridge_ports_if_stale() -> None:
    """Mata bridge(s) zumbi do WhatsApp que ficaram segurando 5050/5100 de um run
    anterior (tipicamente com o Chromium ja morto). Sem isso, o bridge novo nao
    consegue assumir as portas e a Kira acaba falando com o zumbi pra sempre
    (connected=false / browser_alive=false). So mata processos que parecem o
    proprio bridge (whatsapp_bridge/bridge.mjs), nunca processos alheios."""
    try:
        import psutil
    except Exception:
        return
    our_pid = getattr(_whatsapp_proc, "pid", None)
    killed: list[int] = []
    # Mata por PROCESSO (cmdline), nao por porta: no Windows sem admin o
    # psutil.net_connections costuma vir com pid=None, entao matar pelo dono da
    # porta falha silenciosamente. O bridge e sempre `node ...\whatsapp_bridge\bridge.mjs`.
    try:
        procs = list(psutil.process_iter(["pid", "name", "cmdline"]))
    except Exception:
        procs = []
    for proc in procs:
        try:
            info = proc.info if hasattr(proc, "info") else {}
            pid = info.get("pid")
            if not pid or (our_pid and pid == our_pid):
                continue
            name = str(info.get("name") or "").lower()
            cmd = " ".join(info.get("cmdline") or []).lower()
            is_bridge = "node" in name and "whatsapp_bridge" in cmd and "bridge.mjs" in cmd
            if not is_bridge:
                continue
            logger.warning("WHATSAPP_STALE_BRIDGE_KILL pid=%s reason=stale_bridge_process", pid)
            try:
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            proc.kill()
            proc.wait(timeout=5)
            killed.append(int(pid))
            logger.info("WHATSAPP_STALE_BRIDGE_KILLED pid=%s", pid)
        except Exception as exc:
            logger.info("WHATSAPP_STALE_BRIDGE_KILL_FAILED reason=%s", exc)
    if killed:
        time.sleep(1.5)
        logger.info("WHATSAPP_STALE_BRIDGE_PORTS_FREED killed=%s", killed)
    else:
        logger.info("WHATSAPP_STALE_BRIDGE_NONE_FOUND")


def start_whatsapp():
    global _whatsapp_proc
    logger.info("WHATSAPP_OPTIONAL_START")
    logger.info("WHATSAPP_START_REQUEST")
    if _is_whatsapp_process_running():
        logger.info("WHATSAPP_START_ALREADY_RUNNING pid=%s", getattr(_whatsapp_proc, "pid", None))
        return True
    # Limpa bridge zumbi de run anterior que ainda segure 5050/5100 (Chromium morto),
    # senao o bridge novo nao assume as portas e a Kira fala com o zumbi.
    _free_whatsapp_bridge_ports_if_stale()
    node = shutil.which("node")
    if not node:
        _set_whatsapp_runtime_state("error", last_error="Node.js não instalado (nodejs.org)", bridge_available=False)
        logger.warning("WHATSAPP_NODE_MISSING")
        return False
    if not (WHATSAPP_DIR / "node_modules").exists():
        # Primeira vez neste computador: instala as dependências do bridge (~60 MB, ~20 s).
        _set_whatsapp_runtime_state("installing", last_error=None, bridge_available=False)
        logger.info("WHATSAPP_BRIDGE_NPM_INSTALL_BEGIN")
        installed = subprocess.run(
            [shutil.which("npm") or "npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=WHATSAPP_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            shell=sys.platform == "win32",
        )
        if installed.returncode != 0:
            _set_whatsapp_runtime_state("error", last_error="npm install falhou (ver logs_whatsapp)", bridge_available=False)
            logger.warning("WHATSAPP_BRIDGE_NPM_INSTALL_FAILED code=%s tail=%r", installed.returncode, installed.stderr[-300:])
            return False
        logger.info("WHATSAPP_BRIDGE_NPM_INSTALL_OK")
    try:
        _set_whatsapp_runtime_state("booting", last_error=None, bridge_available=False)
        _wpp_env = os.environ.copy()
        _wpp_env["KIRA_INTERNAL_TOKEN"] = KIRA_INTERNAL_TOKEN
        _whatsapp_proc = tracker.start_and_register(
            name="whatsapp_controller",
            args=[node, *WHATSAPP_CMD[1:]],
            cwd=WHATSAPP_DIR,
            env=_wpp_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                          if sys.platform == "win32" else 0,
        )
        print(f"[KIRA] WhatsApp Controller iniciado (PID {_whatsapp_proc.pid})")
        logger.info("WHATSAPP_START_PROCESS_SPAWNED pid=%s", _whatsapp_proc.pid)
        _spawned_proc = _whatsapp_proc
        _bridge_log_path = WHATSAPP_DIR.parent / "logs_whatsapp" / "bridge_runtime.log"

        def _watch():
            # Persiste a saida do bridge num arquivo dedicado (alem do [WPP] no
            # stdout da Kira): login, QR e quedas de conexao ficam registrados.
            try:
                _bridge_log_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            try:
                with open(_bridge_log_path, "a", encoding="utf-8", errors="replace") as _bf:
                    _bf.write(f"\n===== BRIDGE START pid={_spawned_proc.pid} {_now_iso()} =====\n")
                    _bf.flush()
                    for line in _spawned_proc.stdout:
                        print(f"[WPP] {line}", end="")
                        try:
                            _bf.write(line)
                            _bf.flush()
                        except Exception:
                            pass
                    _rc = _spawned_proc.poll()
                    _bf.write(f"===== BRIDGE STDOUT CLOSED exit={_rc} {_now_iso()} =====\n")
            except Exception as _wexc:
                logger.info("WHATSAPP_BRIDGE_WATCH_FAILED reason=%s", _wexc)

        def _liveness():
            # Se o bridge morre logo apos spawnar (ex.: falha de bind/import), o
            # status fica process_running=false "sem motivo". Aqui registramos o
            # exit code explicitamente e apontamos pro log do bridge.
            time.sleep(3.0)
            try:
                _rc = _spawned_proc.poll()
            except Exception:
                _rc = None
            if _rc is not None:
                logger.warning(
                    "WHATSAPP_BRIDGE_DIED_EARLY exit_code=%s ver=%s",
                    _rc, str(_bridge_log_path),
                )
                _set_whatsapp_runtime_state("error", last_error=f"bridge_exit_{_rc}", bridge_available=False)
            else:
                logger.info("WHATSAPP_BRIDGE_ALIVE_AFTER_SPAWN pid=%s", getattr(_spawned_proc, "pid", None))

        threading.Thread(target=_watch, daemon=True).start()
        threading.Thread(target=_liveness, daemon=True).start()
        return True
    except Exception as e:
        _set_whatsapp_runtime_state("error", last_error=str(e), bridge_available=False)
        print(f"[KIRA] Erro ao iniciar WhatsApp Controller: {e}")
        return False


def stop_whatsapp():
    global _whatsapp_proc
    logger.info("WHATSAPP_STOP_REQUEST")
    if _whatsapp_proc and _whatsapp_proc.poll() is None:
        # Signal bridge to stop controller (kills Chrome) before terminating the process
        try:
            import urllib.request as _ur
            _ur.urlopen(_ur.Request("http://127.0.0.1:5050/shutdown", data=b"", headers=_whatsapp_bridge_headers()), timeout=4)
            import time as _t; _t.sleep(1)
            logger.info("WHATSAPP_STOP_BRIDGE_SHUTDOWN_OK")
        except Exception as _sd_exc:
            logger.info("WHATSAPP_STOP_BRIDGE_SHUTDOWN_SKIPPED reason=%s", _sd_exc)
        try:
            if sys.platform == "win32":
                _whatsapp_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _whatsapp_proc.terminate()
            _whatsapp_proc.wait(timeout=5)
        except Exception:
            _whatsapp_proc.kill()
        print("[KIRA] WhatsApp Controller encerrado.")
        logger.info("WHATSAPP_STOP_PROCESS_TERMINATED")
    else:
        logger.info("WHATSAPP_STOP_ALREADY_STOPPED")
    _whatsapp_proc = None
    _set_whatsapp_runtime_state("stopped", bridge_available=False)
    if not _transport_port_open(5050) and not _transport_port_open(5100):
        logger.info("WHATSAPP_STOP_PORTS_RELEASED ports=5050,5100")
    else:
        logger.info("WHATSAPP_STOP_PORTS_PENDING bridge_5050=%s admin_5100=%s",
                    str(_transport_port_open(5050)).lower(), str(_transport_port_open(5100)).lower())


def ensure_whatsapp_started() -> bool:
    from services.feature_runtime_registry import mark_feature_error, mark_feature_running, mark_feature_starting

    # FASE 2B.2/C5: ao ligar o WhatsApp (toggle ou autostart), garante o healthcheck.
    # O loop so age quando enabled; se desligar, ele para de acordar o processo.
    _ensure_whatsapp_healthcheck_started()
    if _is_whatsapp_process_running():
        if not bool(get_whatsapp_runtime_state().get("bridge_available")):
            _request_whatsapp_bridge_start()
        mark_feature_running("whatsapp", reason="already_running")
        return True
    mark_feature_starting("whatsapp", reason="user_requested")
    started = start_whatsapp()
    if not started:
        mark_feature_error("whatsapp", reason="start_failed")
        return False
    _request_whatsapp_bridge_start()
    mark_feature_running("whatsapp", reason="user_requested")
    return True


def ensure_whatsapp_stopped() -> None:
    from services.feature_runtime_registry import mark_feature_stopped

    stop_whatsapp()
    mark_feature_stopped("whatsapp", reason="no_activity")


def _trigger_whatsapp_bridge():
    """
    Aguarda o WhatsApp Controller subir e chama /start_bridge automaticamente.
    Tenta por ate 60 segundos com intervalo de 2 segundos.
    Roda em thread daemon para nao bloquear o startup.
    """
    import time as _time
    try:
        import httpx as _httpx
    except ImportError:
        _set_whatsapp_runtime_state("error", last_error="httpx nao instalado", bridge_available=False)
        print("[KIRA] httpx nao instalado - bridge nao sera acionada automaticamente.")
        return False

    deadline = _time.time() + 120
    while _time.time() < deadline:
        if not _is_whatsapp_process_running():
            _set_whatsapp_runtime_state("stopped", bridge_available=False)
            return False
        try:
            r = _httpx.get("http://127.0.0.1:5100/start_bridge", timeout=3.0)
            if r.status_code == 200:
                _set_whatsapp_runtime_state("bridge_online", last_error=None, bridge_available=True)
                print("[KIRA] Bridge WhatsApp acionada automaticamente.")
                return True
        except Exception:
            pass
        _time.sleep(2)
    _set_whatsapp_runtime_state("error", last_error="bridge nao respondeu em 120s", bridge_available=False)
    print("[KIRA] Aviso: bridge WhatsApp nao respondeu em 120s - veja logs_whatsapp/bridge_runtime.log.")
    return False


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  ÃCONE DA BANDEJA â€” gerado via PIL
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def create_tray_icon_image():
    from PIL import Image, ImageDraw
    if APP_ICON_PATH.exists():
        try:
            return Image.open(APP_ICON_PATH).convert("RGBA")
        except Exception:
            pass
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # CÃ­rculo roxo com "K" branco
    draw.ellipse([2, 2, size-2, size-2], fill="#7c6af7")
    draw.text((18, 14), "K", fill="white")
    return img


def restart_interface_only(icon=None, item=None):
    """
    Reinicia apenas a interface visual da Kira, preservando backend e agent.
    """
    del icon, item
    logger.info("=" * 60)
    logger.info("[KIRA TRAY] restart_interface_requested")
    logger.info("=" * 60)

    reload_url = None
    try:
        logger.info("[KIRA UI] restart_start")

        global _webview_win
        if _webview_win is None:
            logger.warning("[KIRA UI] window_not_found")
            logger.info("[KIRA UI] backend_preserved")
            logger.info("[KIRA UI] agent_preserved")
            logger.info("=" * 60)
            return

        logger.info("[KIRA UI] reload_webview")
        timestamp = int(time.time() * 1000)
        current_url = str(
            getattr(_webview_win, "real_url", "")
            or getattr(_webview_win, "url", "")
            or f"http://127.0.0.1:{PORT}/"
        ).strip()
        parts = urlsplit(current_url or f"http://127.0.0.1:{PORT}/")
        query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "reload"]
        query_items.append(("reload", str(timestamp)))
        reload_url = urlunsplit((
            parts.scheme or "http",
            parts.netloc or f"127.0.0.1:{PORT}",
            parts.path or "/",
            urlencode(query_items),
            parts.fragment,
        ))

        _webview_win.load_url(reload_url)
        _runtime_log("INTERFACE_RESTART_REQUESTED", reload_url=reload_url)

        logger.info("[KIRA UI] reload_url=%s", reload_url)
        logger.info("[KIRA UI] restart_done")
        logger.info("[KIRA UI] backend_preserved")
        logger.info("[KIRA UI] agent_preserved")
        logger.info("=" * 60)
    except Exception as e:
        logger.error("[KIRA UI] restart_failed: %s", e)
        logger.exception("Stack trace:")
        if reload_url:
            _runtime_log("INTERFACE_RESTART_FAILED", reload_url=reload_url, error=str(e))


def request_full_restart(*, reason: str = "user_requested") -> bool:
    """Agenda o relancamento completo usando o mesmo launcher do usuario."""
    script_path = BASE_DIR / "scripts" / "restart_kira.cmd"
    if os.name != "nt" or not script_path.exists():
        logger.warning("KIRA_FULL_RESTART_REJECTED reason=unsupported_platform_or_script_missing")
        return False
    try:
        creation_flags = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        creation_flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        # Sem console (DETACHED) e sem saída, o cmd morria antes do Kira.bat: a Kira fechava e não voltava.
        launcher_log_path = BASE_DIR / "logs" / "Backend" / "restart_launcher.log"
        launcher_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(launcher_log_path, "a", encoding="utf-8", errors="replace") as launcher_log:
            subprocess.Popen(
                [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", str(script_path)],
                cwd=str(BASE_DIR),
                creationflags=creation_flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=launcher_log,
                stderr=subprocess.STDOUT,
            )
        logger.warning("KIRA_FULL_RESTART_SCHEDULED reason=%s", reason)
        _runtime_log("KIRA_FULL_RESTART_SCHEDULED", reason=reason)
        threading.Timer(0.8, _kira_shutdown_handler, kwargs={"signum": "api_restart"}).start()
        return True
    except Exception as exc:
        logger.exception("KIRA_FULL_RESTART_FAILED reason=%s error=%s", reason, exc)
        return False


def build_tray():
    import pystray
    from pystray import MenuItem as Item

    def on_abrir(icon, item):
        if _webview_win:
            _webview_win.show()

    def on_encerrar_sessao(icon, item):
        """Mantida mas nÃ£o exposta no tray (v2.0)."""
        if _webview_win:
            _webview_win.evaluate_js("if(window.disconnect) disconnect();")

    def on_fechar(icon, item):
        """Encerra tudo: tray, janela, agente e o prÃ³prio processo."""
        try:
            icon.stop()
        except Exception:
            pass
        try:
            if _kb is not None:
                _kb.shutdown()
        except Exception:
            pass
        stop_agent()
        stop_whatsapp()
        tracker.cleanup_all()
        try:
            if _webview_win:
                _webview_win.destroy()
        except Exception:
            pass
        os._exit(0)

    _console_visible = False

    def on_toggle_console(icon, item):
        """Mantida mas nÃ£o exposta no tray (v2.0)."""
        nonlocal _console_visible
        if _console_visible:
            hide_console()
            _console_visible = False
        else:
            show_console()
            _console_visible = True

    def console_label(item):
        return "Esconder logs" if _console_visible else "Ver logs (CMD)"

    # Menu minimalista (v2.0) - removido: Reiniciar interface, Encerrar sessÃ£o, Ver logs (CMD)
    menu = pystray.Menu(
        Item("Abrir Kira",        on_abrir, default=True),
        pystray.Menu.SEPARATOR,
        Item("Fechar",            on_fechar),
    )

    icon_img = create_tray_icon_image()
    return pystray.Icon("Kira", icon_img, "Kira", menu)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  JANELA NATIVA (PyWebView)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

WINDOW_STATE_VERSION = 1
WINDOW_DEFAULT_WIDTH = 1100
WINDOW_DEFAULT_HEIGHT = 700
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 550


def _read_window_state():
    try:
        with WINDOW_STATE_PATH.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        if not isinstance(state, dict) or state.get("version") != WINDOW_STATE_VERSION:
            return None
        if type(state.get("maximized")) is not bool:
            return None
        values = {key: state.get(key) for key in ("x", "y", "width", "height")}
        if any(type(value) is not int for value in values.values()):
            return None
        return {"version": WINDOW_STATE_VERSION, "maximized": state["maximized"], **values}
    except Exception:
        return None


def _window_state_create_config(webview, state):
    fallback = {
        "create": {"width": WINDOW_DEFAULT_WIDTH, "height": WINDOW_DEFAULT_HEIGHT},
        "normal_rect": None,
    }
    if state is None:
        return fallback

    screens = []
    try:
        screens = list(webview.screens() or [])
    except Exception:
        screens = []

    width = state["width"]
    height = state["height"]
    if width < WINDOW_MIN_WIDTH or height < WINDOW_MIN_HEIGHT:
        return fallback
    if screens and (
        width > max(int(screen.width) for screen in screens)
        or height > max(int(screen.height) for screen in screens)
    ):
        return fallback

    normal_rect = {key: state[key] for key in ("x", "y", "width", "height")}
    if not screens:
        return {
            "create": {**normal_rect, "maximized": state["maximized"]},
            "normal_rect": normal_rect,
        }

    def intersection_area(screen):
        left = max(state["x"], int(screen.x))
        top = max(state["y"], int(screen.y))
        right = min(state["x"] + width, int(screen.x) + int(screen.width))
        bottom = min(state["y"] + height, int(screen.y) + int(screen.height))
        return max(0, right - left) * max(0, bottom - top)

    target_screen = max(screens, key=intersection_area)
    if intersection_area(target_screen) == 0:
        target_screen = screens[0]
        x = int(target_screen.x) + max((int(target_screen.width) - width) // 2, 0)
        y = int(target_screen.y) + max((int(target_screen.height) - height) // 2, 0)
    else:
        x = state["x"]
        y = state["y"]

    return {
        "create": {
            "screen": target_screen,
            "x": x - int(target_screen.x),
            "y": y - int(target_screen.y),
            "width": width,
            "height": height,
            "maximized": state["maximized"],
        },
        "normal_rect": {"x": x, "y": y, "width": width, "height": height},
    }


def _write_window_state(state):
    temporary_path = None
    try:
        WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(WINDOW_STATE_PATH.parent),
            prefix=".window_state.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(state, temporary_file, ensure_ascii=False, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, WINDOW_STATE_PATH)
    except Exception:
        logger.warning("WINDOW_STATE_SAVE_FAILED", exc_info=True)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass


def _main_window_hidden() -> bool:
    """A janela principal da Kira está minimizada ou escondida na bandeja (Windows)."""
    if sys.platform != "win32" or _webview_win is None:
        return False
    try:
        native = getattr(_webview_win, "native", None)
        handle = int(native.winId()) if native is not None else 0
        if not handle:
            return False
        user32 = ctypes.windll.user32
        return bool(user32.IsIconic(handle)) or not bool(user32.IsWindowVisible(handle))
    except Exception:
        return False


def _open_satellite_window(window_id: str, title: str, url: str, geometry: dict) -> object:
    """Cria uma janela real do Windows (Qt) em tempo de execução; só a janela 1 mantém LiveKit e áudio."""
    import webview

    if _webview_win is None:
        raise RuntimeError("janela principal indisponivel")
    kwargs = dict(title=title, url=url, width=geometry["width"], height=geometry["height"], min_size=(280, 200), resizable=not geometry.get("float"), text_select=False)
    # janela flutuante (Kira minimizada mostrando algo): por cima de qualquer programa, jogo ou navegador
    if geometry.get("on_top"):
        kwargs["on_top"] = True
    if geometry.get("float"):  # só o painel: sem borda, fundo transparente (arrasta pelo cabeçalho do painel)
        kwargs.update(frameless=True, transparent=True, easy_drag=False)
        revealed = threading.Event()

        def hide_from_taskbar(*_):  # como o Radial (ShowInTaskbar=False): janela de ferramenta, fora da barra e do Alt+Tab
            # A janela nasce oculta (hidden=True) e só aparece aqui, já com o estilo final e a página carregada:
            # antes ela surgia, sumia (ShowWindow 0), trocava o estilo e voltava — a piscada com a Kira minimizada.
            if revealed.is_set():
                return
            revealed.set()
            try:
                hwnd, user32 = int(window.native.winId()), ctypes.windll.user32
                user32.SetWindowLongW(hwnd, -20, (user32.GetWindowLongW(hwnd, -20) | 0x80) & ~0x40000)
                user32.ShowWindow(hwnd, 4)  # mostra sem roubar o foco do programa aberto
            except Exception:
                return
            threading.Thread(target=click_through, args=(hwnd,), name="kira-float-click", daemon=True).start()

        def click_through(hwnd: int) -> None:
            """Sobreposição do monitor inteiro: fora dos painéis o clique atravessa (WS_EX_TRANSPARENT) para a área de
            trabalho; com o mouse sobre um painel (ou arrastando um) a janela volta a receber o clique."""
            from ctypes import wintypes

            user32, point, rect = ctypes.windll.user32, wintypes.POINT(), wintypes.RECT()
            while user32.IsWindow(hwnd):
                user32.GetCursorPos(ctypes.byref(point))
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                x, y = point.x - rect.left, point.y - rect.top
                record = _satellite_registry._windows.get(window_id) if _satellite_registry else None
                style = user32.GetWindowLongW(hwnd, -20)
                dragging = not style & 0x20 and user32.GetAsyncKeyState(1) & 0x8000
                inside = dragging or any(a <= x <= c and b <= y <= d for a, b, c, d in getattr(record, "hit", ()))
                wanted = style & ~0x20 if inside else style | 0x20
                if wanted != style:
                    user32.SetWindowLongW(hwnd, -20, wanted)
                time.sleep(0.04)
    if "x" in geometry and "y" in geometry:
        kwargs.update(x=geometry["x"], y=geometry["y"])
    if geometry.get("float"):
        kwargs["hidden"] = True
    window = webview.create_window(**kwargs)
    if geometry.get("float"):
        window.events.loaded += hide_from_taskbar
        # rede da página travada não pode deixar o painel invisível para sempre
        threading.Timer(4.0, hide_from_taskbar).start()
    registry = _satellite_registry
    window.events.closed += lambda *_: registry.note_closed(window_id)
    # minimizada não recebe conteúdo da Kira (vai para a principal); restaurada volta a receber
    window.events.minimized += lambda *_: registry.note_state(window_id, "minimizada")
    window.events.restored += lambda *_: registry.note_state(window_id, "aberta")
    window.events.maximized += lambda *_: registry.note_state(window_id, "maximizada")
    window.events.moved += lambda x, y: registry.note_geometry(window_id, x=int(x), y=int(y))
    window.events.resized += lambda width, height: registry.note_geometry(window_id, width=int(width), height=int(height))
    return window


def _open_startup_windows_threadsafe() -> None:
    registry, loop = _satellite_registry, _backend_loop
    if registry is not None and loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(registry.open_startup(), loop)


# Processo de desenho de uma janela caiu (falta de memória, por exemplo): ela ficava preta e sem reação até fechar a
# Kira. Agora recarrega sozinha, no máximo 3 vezes em 5 minutos por janela (depois disso fica como está e vai pro log).
RENDERER_RELOAD_LIMIT = 3
RENDERER_RELOAD_WINDOW_S = 300.0
_renderer_watch_timers: list[object] = []


def _reload_windows_when_renderer_dies(anchor: object) -> None:
    """Liga o aviso de queda do processo de desenho em cada janela (as extras nascem depois: confere a cada 5 s)."""
    try:
        from qtpy.QtCore import QTimer
        from webview.platforms.qt import BrowserView
    except Exception as exc:
        logger.info("WEBENGINE_RENDERER_WATCH_SKIPPED reason=%s", type(exc).__name__)
        return

    def hook(view: object) -> None:
        page = view.webview.page()
        if getattr(page, "_kira_renderer_watch", False):
            return
        page._kira_renderer_watch = True
        restarts: list[float] = []

        def on_gone(status: object, exit_code: int) -> None:
            now = time.monotonic()
            restarts[:] = [at for at in restarts if now - at < RENDERER_RELOAD_WINDOW_S]
            reload_now = len(restarts) < RENDERER_RELOAD_LIMIT
            if reload_now:
                restarts.append(now)
            logger.warning(
                "WEBENGINE_RENDERER_GONE title=%s status=%s code=%s reload=%s",
                view.windowTitle() if callable(getattr(view, "windowTitle", None)) else "?",
                getattr(status, "name", status), exit_code, str(reload_now).lower(),
            )
            if reload_now:
                QTimer.singleShot(1500, view.webview.reload)

        page.renderProcessTerminated.connect(on_gone)

    def hook_all() -> None:
        for view in list(BrowserView.instances.values()):
            try:
                hook(view)
            except Exception as exc:
                logger.info("WEBENGINE_RENDERER_WATCH_FAILED reason=%s", type(exc).__name__)

    def start() -> None:
        timer = QTimer()
        timer.timeout.connect(hook_all)
        timer.start(5000)
        _renderer_watch_timers.append(timer)
        hook_all()

    QTimer.singleShot(0, anchor, start)


def _keep_app_alive_when_windows_close() -> None:
    """A janela 1 vai para a bandeja; fechar o último satélite visível não pode encerrar o app Qt."""
    try:
        from qtpy.QtWidgets import QApplication

        instance = QApplication.instance()
        if instance is not None:
            instance.setQuitOnLastWindowClosed(False)
    except Exception:
        logger.warning("QT_QUIT_ON_LAST_WINDOW_UNCHANGED", exc_info=True)


def _close_satellites_threadsafe() -> None:
    registry, loop = _satellite_registry, _backend_loop
    if registry is not None and loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(registry.close_all(), loop)


# Raster na CPU com mais threads: seguro em qualquer placa e recupera parte da fluidez.
# --disable-accelerated-2d-canvas: o canvas 2D dos orbes ia para a GTX 550 Ti (GPU dentro do python.exe) e a Kira caía no
# Qt6WebEngineCore +0xfc51b3 (3 quedas em 12 min em 2026-11-05 com o orbe Nuvem). No processador o orbe custa < 1 ms.
# WebGL (galáxia, globo, Olho de Deus) continua na placa. KIRA_GPU_CANVAS=1 volta o canvas para a GPU.
WEBENGINE_RASTER_THREAD_FLAGS = ("--num-raster-threads=4",) + (
    () if os.getenv("KIRA_GPU_CANVAS", "0").strip() in {"1", "true", "on"} else ("--disable-accelerated-2d-canvas",))
# Forçar raster na GPU passando por cima da blocklist. Desligado por padrão: o Chromium bloqueia raster na GPU em placas
# NVIDIA Fermi (GTX 550 Ti desta máquina, crbug 1047002) e, com a GPU rodando dentro do python.exe, isso derrubava a Kira
# inteira (2026-10-21 19:29 e 22:07, violação de acesso no mesmo ponto do Qt6WebEngineCore.dll).
WEBENGINE_FORCED_GPU_RASTER_FLAGS = ("--ignore-gpu-blocklist", "--enable-gpu-rasterization")


def _apply_webengine_gpu_flags() -> None:
    """KIRA_GPU_RASTER=1 força o raster na GPU (só para placas fora da blocklist do Chromium)."""
    wanted = list(WEBENGINE_RASTER_THREAD_FLAGS)
    if str(os.getenv("KIRA_GPU_RASTER", "0")).strip().lower() in {"1", "true", "yes", "on"}:
        wanted += WEBENGINE_FORCED_GPU_RASTER_FLAGS
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    present = {flag.split("=", 1)[0] for flag in flags}
    flags += [flag for flag in wanted if flag.split("=", 1)[0] not in present]
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    logger.info("WEBENGINE_GPU_RASTER flags=%s", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])


def _keep_screen_share_picker() -> None:
    """O pywebview liga --use-fake-ui-for-media-stream: o compartilhamento de tela escolhia sozinho a tela principal
    (onde fica a própria Kira, que só via a interface dela) e o seletor nunca aparecia. Sem a flag, as permissões de
    microfone, câmera e tela passam por services.screen_share_picker."""
    from webview.platforms import qt as qt_platform

    original = qt_platform.environ_append

    def environ_append(key, *values, **kwargs):
        return original(key, *[value for value in values if value != "--use-fake-ui-for-media-stream"], **kwargs)

    qt_platform.environ_append = environ_append


def run_window():
    global _webview_win, _browser_webview_win, _tray_icon, _browser_host_available

    _apply_webengine_gpu_flags()
    _keep_screen_share_picker()
    import webview

    # Aguarda o servidor FastAPI ficar pronto
    _server_ready.wait(timeout=10)
    time.sleep(0.3)

    window_state_config = _window_state_create_config(webview, _read_window_state())
    window_kwargs = dict(
        title="Kira",
        url=f"http://127.0.0.1:{PORT}{desktop_ui_query(os.getenv('KIRA_UI_ENABLED'))}",
        width=1100,
        height=700,
        min_size=(800, 550),
        resizable=True,
        on_top=False,
        # Sem moldura padrÃ£o do browser â€” parece app nativo
        text_select=False,
    )
    window_kwargs.update(window_state_config["create"])
    if APP_ICON_PATH.exists():
        window_kwargs["icon"] = str(APP_ICON_PATH)

    try:
        _webview_win = webview.create_window(**window_kwargs)
    except TypeError:
        window_kwargs.pop("icon", None)
        _webview_win = webview.create_window(**window_kwargs)
    if KIRA_BOOT_MINIMAL:
        logger.info("BOOT_MINIMAL_UI_READY")
    _apply_windows_icon_to_window()

    window_state_lock = threading.Lock()
    window_state_save_timer = None
    window_state_normal_rect = dict(window_state_config["normal_rect"] or {})
    window_state_maximized = bool(window_state_config["create"].get("maximized", False))
    window_state_minimized = False
    window_state_tracking_enabled = False

    def _window_state_native_flags():
        native = getattr(_webview_win, "native", None)
        try:
            is_maximized = bool(native.isMaximized())
        except Exception:
            is_maximized = window_state_maximized
        try:
            is_minimized = bool(native.isMinimized())
        except Exception:
            is_minimized = window_state_minimized
        try:
            native_handle = int(native.winId())
            is_maximized = is_maximized or bool(ctypes.windll.user32.IsZoomed(native_handle))
            is_minimized = is_minimized or bool(ctypes.windll.user32.IsIconic(native_handle))
        except Exception:
            pass
        return is_maximized, is_minimized

    def _window_state_size_looks_maximized(width, height):
        try:
            screens = list(webview.screens() or [])
        except Exception:
            return False
        return any(
            width >= int(screen.width) - 2 and height >= int(screen.height) - 100
            for screen in screens
        )

    def _window_state_capture_normal_rect():
        nonlocal window_state_normal_rect
        is_maximized, is_minimized = _window_state_native_flags()
        if is_maximized or is_minimized:
            return
        try:
            native = getattr(_webview_win, "native", None)
            frame_geometry = native.frameGeometry() if native is not None else None
            width = int(_webview_win.width)
            height = int(_webview_win.height)
            if _window_state_size_looks_maximized(width, height):
                return
            window_state_normal_rect = {
                "x": int(frame_geometry.x()) if frame_geometry is not None else int(_webview_win.x),
                "y": int(frame_geometry.y()) if frame_geometry is not None else int(_webview_win.y),
                "width": width,
                "height": height,
            }
        except Exception:
            pass

    def _window_state_save_now():
        nonlocal window_state_maximized
        is_maximized, is_minimized = _window_state_native_flags()
        if is_minimized:
            return
        if not window_state_normal_rect:
            _window_state_capture_normal_rect()
        if not window_state_normal_rect:
            return
        window_state_maximized = is_maximized
        _write_window_state(
            {
                "version": WINDOW_STATE_VERSION,
                "maximized": bool(window_state_maximized),
                **window_state_normal_rect,
            }
        )

    def _window_state_debounced_save(*_args):
        nonlocal window_state_save_timer
        with window_state_lock:
            if window_state_save_timer is not None:
                window_state_save_timer.cancel()
            window_state_save_timer = threading.Timer(0.5, _window_state_save_now)
            window_state_save_timer.daemon = True
            window_state_save_timer.start()

    def _window_state_on_resized(*_args):
        if not window_state_tracking_enabled:
            return
        _window_state_capture_normal_rect()
        _window_state_debounced_save()

    def _window_state_on_moved(*_args):
        if not window_state_tracking_enabled:
            return
        _window_state_capture_normal_rect()
        _window_state_debounced_save()

    def _window_state_on_maximized(*_args):
        nonlocal window_state_maximized, window_state_minimized
        if not window_state_tracking_enabled:
            return
        window_state_maximized = True
        window_state_minimized = False
        _window_state_debounced_save()

    def _window_state_on_minimized(*_args):
        nonlocal window_state_minimized
        if not window_state_tracking_enabled:
            return
        window_state_minimized = True

    def _window_state_on_restored(*_args):
        nonlocal window_state_maximized, window_state_minimized
        if not window_state_tracking_enabled:
            return
        window_state_maximized = False
        window_state_minimized = False
        if window_state_normal_rect:
            _webview_win.move(window_state_normal_rect["x"], window_state_normal_rect["y"])
            _webview_win.resize(window_state_normal_rect["width"], window_state_normal_rect["height"])
        _window_state_capture_normal_rect()
        _window_state_debounced_save()

    def _window_state_on_shown(*_args):
        if not window_state_normal_rect:
            _window_state_capture_normal_rect()
        elif not _window_state_native_flags()[0]:
            _webview_win.move(window_state_normal_rect["x"], window_state_normal_rect["y"])
        threading.Timer(1.0, _window_state_enable_tracking).start()

    def _window_state_enable_tracking():
        nonlocal window_state_tracking_enabled
        window_state_tracking_enabled = True

    _webview_win.events.shown += _window_state_on_shown

    screen_picker_installed = threading.Event()

    def _install_screen_share_picker(*_args):
        view = getattr(getattr(_webview_win, "native", None), "webview", None)
        if view is None or screen_picker_installed.is_set():
            return
        screen_picker_installed.set()
        from PySide6.QtCore import QCoreApplication, QTimer

        from services.screen_share_picker import install

        QTimer.singleShot(0, QCoreApplication.instance(), lambda: install(view))

    _webview_win.events.shown += _install_screen_share_picker

    def _start_global_hotkeys(*_args):
        from services import kira_settings
        from services.global_hotkeys import GlobalHotkeys

        GlobalHotkeys(_webview_win.evaluate_js, lambda: kira_settings.load().get("shortcuts") or {}).start()

    _webview_win.events.shown += _start_global_hotkeys
    _webview_win.events.resized += _window_state_on_resized
    _webview_win.events.moved += _window_state_on_moved
    _webview_win.events.maximized += _window_state_on_maximized
    _webview_win.events.minimized += _window_state_on_minimized
    _webview_win.events.restored += _window_state_on_restored


    def _window_state_on_closing(*_args):
        nonlocal window_state_save_timer
        with window_state_lock:
            if window_state_save_timer is not None:
                window_state_save_timer.cancel()
                window_state_save_timer = None
        _window_state_save_now()

    _webview_win.events.closing += _window_state_on_closing

    if BROWSER_HOST_ENABLED:
        try:
            _browser_webview_win = webview.create_window(
                "Kira Browser Host",
                url="about:blank",
                hidden=True,
                frameless=True,
                width=800,
                height=600,
                background_color="#000000",
            )
            _browser_host_available = True
            logger.info("BROWSER_HOST_CREATED enabled=true")
        except Exception as exc:
            _browser_webview_win = None
            _browser_host_available = False
            logger.warning("BROWSER_HOST_CREATE_FAILED error=%s", exc)
            record_degraded_service("browser", f"browser_host_create_failed:{exc}")
    else:
        _browser_webview_win = None
        _browser_host_available = False
        logger.info("BROWSER_HOST_DISABLED enabled=false reason=boot_stability")
        if is_optional_enabled("browser"):
            record_degraded_service("browser", "browser_host_disabled_during_boot")

    def on_closing():
        """Ao clicar no X: minimiza pra bandeja em vez de fechar."""
        _close_satellites_threadsafe()
        _webview_win.hide()
        return False   # False = cancela o fechamento real

    _webview_win.events.closing += on_closing

    # Inicia a bandeja em thread separada
    _tray_icon = build_tray()
    tray_thread = threading.Thread(target=_tray_icon.run, daemon=True)
    tray_thread.start()

    WEBVIEW_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _runtime_log(
        "[MIC]",
        host_session_persistent="true",
        native_permission_persisted="true",
        storage_path=str(WEBVIEW_STORAGE_DIR),
    )

    def _prepare_youtube_player_on_ui_thread():
        # Player do YouTube do painel: logado (nesta rede, sem login todo vídeo embutido dá erro) e tocando quando a
        # Kira manda por voz (o Qt exige clique do usuário por padrão). O Qt exige a thread da interface.
        try:
            from qtpy.QtCore import QTimer
            from qtpy.QtWebEngineCore import QWebEngineSettings
            from webview.platforms.qt import BrowserView
            from services.youtube_cookies import install_youtube_cookies
        except Exception as exc:
            logger.info("YOUTUBE_PLAYER_PREP_SKIPPED reason=%s", type(exc).__name__)
            return
        views = [view for view in BrowserView.instances.values() if getattr(view, "profile", None) is not None]
        if not views:
            return

        def _install():
            gesture = QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture
            for view in views:
                view.profile.settings().setAttribute(gesture, False)
                view.webview.page().settings().setAttribute(gesture, False)
            try:
                install_youtube_cookies(views)
            except Exception as exc:
                logger.warning("YOUTUBE_COOKIES_FAILED reason=%s", type(exc).__name__)

        QTimer.singleShot(0, views[0], _install)
        _reload_windows_when_renderer_dies(views[0])

    def _on_webview_started():
        _keep_app_alive_when_windows_close()
        _prepare_youtube_player_on_ui_thread()
        threading.Timer(4.0, _open_startup_windows_threadsafe).start()
        if _kb is not None and _browser_webview_win is not None and _browser_host_available:
            _kb.init(_browser_webview_win)
        elif not BROWSER_HOST_ENABLED:
            logger.info("BROWSER_HOST_DESTROYED_IGNORED_FOR_AGENT reason=pre_session_not_user_close")
        else:
            record_degraded_service("browser", "browser_module_unavailable")
        for delay in (0.15, 0.8, 1.8):
            threading.Timer(delay, _apply_windows_icon_to_window).start()

    webview_gui = str(os.getenv("PYWEBVIEW_GUI") or "").strip() or None
    logger.info("PYWEBVIEW_START gui=%s storage_path=%s", webview_gui or "auto", WEBVIEW_STORAGE_DIR)
    webview.start(
        func=_on_webview_started,
        debug=False,
        private_mode=False,
        storage_path=str(WEBVIEW_STORAGE_DIR),
        gui=webview_gui,
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  ENTRY POINT
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _kira_shutdown_handler(signum=None, frame=None):
    """Encerramento ordenado da árvore Kira (SIGTERM/SIGINT)."""
    logger.info('[SHUTDOWN] SHUTDOWN_BEGIN signal=%s', signum)
    try:
        stop_agent()
    except Exception as _e:
        logger.debug('[SHUTDOWN] stop_agent error: %s', _e)
    try:
        stop_whatsapp()
    except Exception as _e:
        logger.debug('[SHUTDOWN] stop_whatsapp error: %s', _e)
    try:
        stop_livekit_server()
    except Exception as _e:
        logger.debug('[SHUTDOWN] stop_livekit_server error: %s', _e)
    try:
        tracker.cleanup_all()
    except Exception as _e:
        logger.debug('[SHUTDOWN] tracker.cleanup_all error: %s', _e)
    logger.info('[SHUTDOWN] SHUTDOWN_COMPLETE')
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _kira_shutdown_handler)
    try:
        signal.signal(signal.SIGINT, _kira_shutdown_handler)
    except (OSError, ValueError):
        pass  # SIGINT pode não ser configurável em threads não-principais
    _configure_backend_file_logging()
    _install_backend_exception_hook()
    _enforce_official_boot_python()
    if tracker.check_already_running():
        print("====================================")
        print("ERRO: Kira ja esta rodando!")
        print("Feche a outra instancia antes de iniciar nova.")
        print("====================================")
        sys.exit(1)
    logger.info("KIRA_BACKEND_PROCESS_SINGLETON_OK pid=%s python=%s", os.getpid(), sys.executable)
    logger.info("=" * 80)
    logger.info("KIRA BACKEND INICIANDO")
    logger.info("=" * 80)
    print("====================================")
    print("KIRA: Instancia unica confirmada")
    print("====================================")
    os.chdir(BASE_DIR)
    _set_windows_app_id()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    APP_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    INTERFACE_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 48)
    print("  KIRA iniciando...")
    print("=" * 48)
    hide_console()  # esconde o CMD imediatamente
    _update_runtime_readiness(
        boot_state="booting",
        ui_status="connecting",
        agent_process_spawned=False,
        agent_connected=False,
        agent_ready=False,
        message="Inicializando backend...",
        event="BOOT_STATE_CHANGE",
        boot_started_at=time.monotonic(),
        room_connecting=False,
        room_connected=False,
        room_ready=False,
        gemini_connecting=False,
        gemini_ready=False,
        runtime_ready=False,
        ui_ready=False,
        backend_ready=False,
        startup_greeting_created=False,
        startup_greeting_sanitized=False,
        startup_greeting_enqueued=False,
        startup_greeting_dispatched=False,
        startup_greeting_speaking=False,
        startup_greeting_done=False,
        functional_ready=False,
        voice_pipeline_ready=False,
        error="",
        failure_reason="",
        boot_recovery_active=False,
    )
    threading.Thread(target=_boot_timeout_watcher, daemon=True).start()

    # 1. Servidor FastAPI em thread
    if not _check_port_available(PORT):
        logger.error("Porta %s ja esta ocupada por outro processo", PORT)
        boot_orchestrator.fail(f"Porta {PORT} ja esta ocupada por outro processo.")
        print(f"[INTERFACE] ERRO: porta {PORT} ja esta ocupada.")
        sys.exit(1)
    print("=" * 60)
    print("INICIANDO INTERFACE DA KIRA")
    print("=" * 60)
    logger.info("[BOOT_CRITICAL_START]")
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    print("[INTERFACE] Aguardando interface ficar pronta...")
    if _server_ready.wait(timeout=12):
        print("[INTERFACE] Interface HTTP pronta.")
    else:
        failure_reason = _server_start_error or "timeout"
        logger.error("[INTERFACE] Servidor HTTP nao sinalizou pronto em 12s. motivo=%s", failure_reason)
        boot_orchestrator.fail(f"Servidor HTTP nao sinalizou pronto: {failure_reason}")
        print(f"[INTERFACE] ERRO: interface nao sinalizou pronta em 12s. motivo={failure_reason}")
        if not server_thread.is_alive():
            print("[INTERFACE] Thread do servidor encerrou antes de abrir a porta.")
        sys.exit(1)
    time.sleep(0.6)
    print("[INTERFACE] Janela sera iniciada apos estabilizacao inicial.")

    logger.info("INICIANDO_LIVEKIT_SERVER")
    livekit_started = ensure_livekit_server_ready()
    if not livekit_started:
        logger.error("LIVEKIT_SERVER_FALHOU - Kira nao funcionara sem LiveKit")
    elif KIRA_BOOT_MINIMAL:
        logger.info("BOOT_MINIMAL_LIVEKIT_READY")


    # 2. Agente em subprocess
    start_agent()
    if _agent_ready.wait(timeout=10):
        print("[AGENT] Worker pronto para sessao.")
        if KIRA_BOOT_MINIMAL:
            logger.info("BOOT_MINIMAL_AGENT_WORKER_READY")
    else:
        print("[AGENT] Aviso: worker ainda nao sinalizou pronto (continuando).")
    time.sleep(1.0)
    snapshot = _augment_runtime_state(_runtime_state_snapshot())
    if bool(snapshot.get("pre_start_ready")) and bool(snapshot.get("can_start_session")):
        logger.info("[BOOT_CRITICAL_READY] pre_start_ready=true can_start_session=true")
        if KIRA_BOOT_MINIMAL:
            logger.info("BOOT_MINIMAL_READY_FOR_SESSION_START")
    else:
        missing = ",".join(snapshot.get("missing_flags") or []) or "unknown"
        logger.info("[BOOT_CRITICAL_WAITING] missing_flags=%s", missing)

    # 3. WhatsApp Controller opcional (controlado por config persistida)
    # BLOCO3: servicos opcionais nao bloqueiam o boot principal
    logger.info("[BOOT_SECONDARY_START]")
    try:
        cfg = load_whatsapp_runtime_config()
        if KIRA_BOOT_MINIMAL:
            _set_whatsapp_runtime_state("disabled", bridge_available=False)
            logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
            logger.info("WHATSAPP_LAZY_READY")
            logger.info("VISION_BOOT_SKIPPED reason=boot_minimal")
            logger.info("VISION_LAZY_READY")
            logger.info("BROWSER_BOOT_SKIPPED reason=boot_minimal")
            logger.info("BROWSER_LAZY_READY")
            logger.info("MUSIC_BOOT_SKIPPED reason=boot_minimal")
            logger.info("MUSIC_LAZY_READY")
            logger.info("MANUS_BOOT_SKIPPED reason=boot_minimal")
            logger.info("MANUS_LAZY_READY")
            logger.info("SYSTEM_VITALS_BOOT_SKIPPED reason=boot_minimal")
            logger.info("SYSTEM_VITALS_LAZY_READY")
            logger.debug("PC_OPERATOR_REMOVED_BOOT_SKIPPED")  # [REMOVED] PC_OPERATOR
        elif not is_optional_enabled("whatsapp"):
            _set_whatsapp_runtime_state("disabled", bridge_available=False)
            logger.info("OPTIONAL_MODULE_SKIPPED_BOOT module=whatsapp reason=config")
        elif KIRA_LAZY_LOAD_EXTRAS:
            if bool(cfg.get("enabled", False)):
                _set_whatsapp_runtime_state("stopped", bridge_available=False)
                logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
                logger.info("WHATSAPP_LAZY_READY")
                logger.info("WHATSAPP_RUNTIME_STATE state=off")
            else:
                _set_whatsapp_runtime_state("disabled", bridge_available=False)
                logger.info("WHATSAPP_LAZY_SKIP disabled=true")
            logger.info("VISION_LAZY_WAIT user_action_required")
            logger.info("BROWSER_LAZY_WAIT user_action_required")
            logger.info("MUSIC_LAZY_WAIT user_action_required")
            logger.info("MAINTENANCE_LAZY_WAIT user_action_required")
        elif bool(cfg.get("enabled", False)):
            _set_whatsapp_runtime_state("stopped", bridge_available=False)
            logger.info("WHATSAPP_BOOT_SKIPPED reason=boot_minimal")
            logger.info("WHATSAPP_LAZY_READY")
            logger.info("WHATSAPP_RUNTIME_STATE state=off")
        else:
            _set_whatsapp_runtime_state("disabled", bridge_available=False)
            logger.info("[BLOCO3] WhatsApp: desabilitado por config")
            logger.info("[BOOT_SECONDARY_DEGRADED service=whatsapp]")
    except Exception as _wpp_boot_err:
        logger.warning("[BLOCO3] WhatsApp: excecao no boot — %s", _wpp_boot_err)
        logger.warning("[BLOCO3] WhatsApp: degradado, Kira segue sem WhatsApp")
        logger.info("[BOOT_SECONDARY_DEGRADED service=whatsapp]")
    logger.info("[BOOT_SECONDARY_READY]")
    for _module_name in ("manus", "browser", "pc_operator", "system_vitals", "music", "vision", "panels", "diagnostics"):
        if not should_autoload(_module_name):
            logger.info("OPTIONAL_MODULE_SKIPPED_BOOT module=%s reason=config", _module_name)

    # 5. Janela nativa (bloqueia atÃ© fechar)
    run_window()

    # 6. Limpeza ao sair
    stop_agent()
    stop_whatsapp()
    stop_livekit_server()
    tracker.cleanup_all()
    print("[KIRA] Encerrado.")
