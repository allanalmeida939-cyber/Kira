from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable

from services.feature_runtime_registry import (
    get_feature_state,
    mark_feature_error,
    mark_feature_running,
    mark_feature_starting,
    mark_feature_state,
    mark_feature_stopped,
)


logger = logging.getLogger("kira.system_vitals_runtime")

Collector = Callable[[], dict[str, Any]]


@dataclass
class SystemVitalsRuntimeState:
    state: str = "off"
    polling: bool = False
    panel_visible: bool = False
    reason: str = "panel_hidden"
    last_error: str | None = None
    last_sample_at: float | None = None
    idle_shutdown_seconds: int = 30


_lock = RLock()
_state = SystemVitalsRuntimeState()


def reset_for_tests() -> None:
    with _lock:
        _state.state = "off"
        _state.polling = False
        _state.panel_visible = False
        _state.reason = "panel_hidden"
        _state.last_error = None
        _state.last_sample_at = None


def _snapshot() -> dict[str, Any]:
    feature = get_feature_state("system_vitals")
    with _lock:
        return {
            "success": True,
            "ok": True,
            "enabled": bool(feature.enabled),
            "state": _state.state,
            "lazy": bool(feature.lazy),
            "autostart": bool(feature.autostart),
            "polling": bool(_state.polling),
            "panel_visible": bool(_state.panel_visible),
            "reason": _state.reason,
            "last_error": _state.last_error,
            "last_sample_at": _state.last_sample_at,
            "timestamp": time.time(),
        }


def get_system_vitals_status(light: bool = True) -> dict[str, Any]:
    if light:
        logger.info("SYSTEM_VITALS_STATUS_REQUESTED light=true")
        logger.info("SYSTEM_VITALS_STATUS_NO_WAKEUP")
    return _snapshot()


async def ensure_system_vitals_started(reason: str) -> dict[str, Any]:
    with _lock:
        _state.state = "starting"
        _state.reason = reason
        _state.last_error = None
    mark_feature_starting("system_vitals", reason=reason)
    mark_feature_running("system_vitals", reason=reason)
    with _lock:
        _state.state = "polling"
        _state.polling = True
        _state.panel_visible = reason in {"panel_visible", "ui_panel_show"}
    mark_feature_state("system_vitals", "polling", reason=reason)
    logger.info("SYSTEM_VITALS_POLLING_START reason=%s", reason)
    return _snapshot()


async def collect_system_vitals(*, active: bool = False, reason: str = "panel_visible", collector: Collector | None = None) -> dict[str, Any]:
    if not active:
        return get_system_vitals_status(light=True)
    await ensure_system_vitals_started(reason)
    try:
        if collector is None:
            from services.system_vitals import get_system_vitals_cached

            payload = get_system_vitals_cached(min_interval_seconds=2.0, force=True, scope=reason)
        else:
            payload = collector()
        with _lock:
            _state.last_sample_at = time.time()
            _state.last_error = None
        if isinstance(payload, dict):
            payload.setdefault("success", True)
            payload.setdefault("ok", True)
            payload.setdefault("state", "polling")
            payload.setdefault("polling", True)
        return payload
    except Exception as exc:
        with _lock:
            _state.state = "error"
            _state.polling = False
            _state.last_error = str(exc)
            _state.reason = reason
        mark_feature_error("system_vitals", reason=str(exc))
        logger.info("SYSTEM_VITALS_COLLECT_FAILED_NON_FATAL reason=%s", exc)
        payload = _snapshot()
        payload.update({"success": False, "ok": False, "error": str(exc), "stale": True})
        return payload


async def stop_system_vitals(reason: str) -> dict[str, Any]:
    logger.info("SYSTEM_VITALS_POLLING_STOP reason=%s", reason)
    with _lock:
        _state.state = "off"
        _state.polling = False
        _state.panel_visible = False
        _state.reason = reason
    mark_feature_stopped("system_vitals", reason=reason)
    return _snapshot()


def set_panel_visible(visible: bool, *, reason: str) -> dict[str, Any]:
    with _lock:
        _state.panel_visible = bool(visible)
        _state.reason = reason
        if not visible:
            _state.polling = False
            _state.state = "off"
    return _snapshot()


def is_system_vitals_polling() -> bool:
    with _lock:
        return bool(_state.polling)
