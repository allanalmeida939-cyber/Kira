from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any


logger = logging.getLogger("kira.feature_runtime")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURES_CONFIG_PATH = PROJECT_ROOT / "configs" / "kira_features.json"
EXTRA_FEATURES = ("manus", "internet", "messages", "vision", "music", "system_vitals")
VALID_STATES = {"off", "idle", "starting", "running", "playing", "paused", "polling", "stopping", "error"}


@dataclass
class FeatureRuntimeState:
    name: str
    state: str = "off"
    autostart: bool = False
    lazy: bool = True
    enabled: bool = True
    last_started_at: float | None = None
    last_stopped_at: float | None = None
    reason: str | None = None
    idle_shutdown_seconds: int | None = None
    shutdown_after_task: bool = False
    provider: str | None = None
    legacy_providers_disabled: bool = False


_lock = RLock()
_states: dict[str, FeatureRuntimeState] = {}
_initialized = False


def _read_config() -> dict[str, Any]:
    try:
        return json.loads(FEATURES_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("FEATURE_RUNTIME_CONFIG_READ_FAILED reason=%s", exc)
        return {}


def _feature_config(config: dict[str, Any], name: str) -> dict[str, Any]:
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    optional = config.get("optional_modules") if isinstance(config.get("optional_modules"), dict) else {}
    legacy_autostart = bool(features.get(f"{name}_autostart", False))
    rich = features.get(name) if isinstance(features.get(name), dict) else {}
    opt = optional.get(name) if isinstance(optional.get(name), dict) else {}
    return {
        "enabled": bool(rich.get("enabled", opt.get("enabled", True))),
        "autostart": bool(rich.get("autostart", legacy_autostart or opt.get("autoload", False))),
        "lazy": bool(rich.get("lazy", True)),
        "idle_shutdown_seconds": rich.get("idle_shutdown_seconds"),
        "shutdown_after_task": bool(rich.get("shutdown_after_task", False)),
        "provider": rich.get("provider"),
        "legacy_providers_disabled": bool(rich.get("legacy_providers_disabled", False)),
    }


def initialize_feature_runtime_registry(*, force: bool = False) -> dict[str, FeatureRuntimeState]:
    global _initialized
    with _lock:
        if _initialized and not force:
            return dict(_states)
        config = _read_config()
        _states.clear()
        for name in EXTRA_FEATURES:
            cfg = _feature_config(config, name)
            autostart = bool(cfg["autostart"])
            lazy = bool(cfg["lazy"])
            state = "idle" if autostart and not lazy else "off"
            _states[name] = FeatureRuntimeState(
                name=name,
                state=state,
                autostart=autostart,
                lazy=lazy,
                enabled=bool(cfg["enabled"]),
                reason="autostart_configured" if autostart else "boot_minimal",
                idle_shutdown_seconds=(
                    int(cfg["idle_shutdown_seconds"])
                    if str(cfg.get("idle_shutdown_seconds") or "").strip().isdigit()
                    else None
                ),
                shutdown_after_task=bool(cfg["shutdown_after_task"]),
                provider=str(cfg["provider"]) if cfg.get("provider") else None,
                legacy_providers_disabled=bool(cfg.get("legacy_providers_disabled")),
            )
        _initialized = True
        logger.info("FEATURE_RUNTIME_REGISTRY_READY features=%s", ",".join(EXTRA_FEATURES))
        for name in EXTRA_FEATURES:
            if name == "vision":
                logger.info("VISION_BOOT_SKIPPED reason=boot_minimal")
                logger.info("VISION_PROVIDER_SELECTED provider=%s", _states[name].provider or "livekit")
                logger.info("VISION_LEGACY_PROVIDER_DISABLED provider=dxcam")
                logger.info("VISION_LEGACY_SERVER_DISABLED")
            if name == "system_vitals":
                logger.info("SYSTEM_VITALS_BOOT_SKIPPED reason=boot_minimal")
                logger.info("SYSTEM_VITALS_LAZY_READY")
                logger.info("SYSTEM_VITALS_RUNTIME_STATE state=off")
            elif name == "internet":
                logger.info("INTERNET_BOOT_SKIPPED reason=boot_minimal")
                logger.info("INTERNET_LAZY_READY")
                logger.info("INTERNET_RUNTIME_STATE state=off")
            elif name == "messages":
                logger.info("MESSAGES_BOOT_SKIPPED reason=boot_minimal")
                logger.info("MESSAGES_LAZY_READY")
                logger.info("MESSAGES_RUNTIME_STATE state=off")
            elif name == "music":
                logger.info("MUSIC_BOOT_SKIPPED reason=boot_minimal")
                logger.info("MUSIC_LAZY_READY")
                logger.info("MUSIC_RUNTIME_STATE state=off")
            elif name == "manus":
                logger.info("MANUS_BOOT_SKIPPED reason=boot_minimal")
        logger.info("PC_OPERATOR_REMAINS_DISABLED")
        return dict(_states)


def get_feature_state(name: str) -> FeatureRuntimeState:
    initialize_feature_runtime_registry()
    key = str(name or "").strip().lower()
    with _lock:
        return _states.get(key, FeatureRuntimeState(name=key or "unknown", state="off", enabled=False, reason="unknown_feature"))


def mark_feature_state(name: str, state: str, *, reason: str | None = None) -> FeatureRuntimeState:
    if state not in VALID_STATES:
        state = "error"
        reason = reason or "invalid_state"
    initialize_feature_runtime_registry()
    key = str(name or "").strip().lower()
    now = time.time()
    with _lock:
        current = _states.get(key) or FeatureRuntimeState(name=key, enabled=False, reason="unknown_feature")
        current.state = state
        current.reason = reason
        if state == "running":
            current.last_started_at = now
        if state in {"off", "idle"}:
            current.last_stopped_at = now
        _states[key] = current
        return current


def mark_feature_starting(name: str, *, reason: str) -> FeatureRuntimeState:
    logger.info("%s_LAZY_START_BEGIN reason=%s", str(name).upper(), reason)
    return mark_feature_state(name, "starting", reason=reason)


def mark_feature_running(name: str, *, reason: str = "") -> FeatureRuntimeState:
    if str(name).strip().lower() == "vision":
        logger.info("VISION_LAZY_START_OK provider=livekit")
    else:
        logger.info("%s_LAZY_START_OK", str(name).upper())
    return mark_feature_state(name, "running", reason=reason or "lazy_started")


def mark_feature_stopped(name: str, *, reason: str) -> FeatureRuntimeState:
    logger.info("%s_LAZY_SHUTDOWN_BEGIN reason=%s", str(name).upper(), reason)
    state = mark_feature_state(name, "off", reason=reason)
    logger.info("%s_LAZY_SHUTDOWN_OK", str(name).upper())
    return state


def mark_feature_error(name: str, *, reason: str) -> FeatureRuntimeState:
    logger.info("%s_LAZY_START_FAILED_NON_FATAL reason=%s", str(name).upper(), reason)
    return mark_feature_state(name, "error", reason=reason)


def feature_runtime_status() -> dict[str, dict[str, Any]]:
    initialize_feature_runtime_registry()
    with _lock:
        return {name: asdict(state) for name, state in _states.items()}
