from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


logger = logging.getLogger("kira.app")

# Cache em memoria da config, chaveado por caminho, invalidado pelo mtime do
# arquivo. Antes, load_economy_mode_config lia (e as vezes reescrevia) o arquivo
# em disco a CADA chamada -- ~546 vezes numa sessao de ~90min, no event loop,
# contribuindo para travamentos do loop (job executor unresponsive). Com o cache,
# leituras repetidas com o arquivo inalterado nao tocam o disco nem re-normalizam.
_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "economy_mode.json"

DEFAULT_ECONOMY_MODE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "mode": "gemini_only",
    "livekit_local_default": True,
    "livekit_cloud_disabled_by_default": True,
    "livekit_cloud_fallback_enabled": True,
    "cloud_fallback_mode": "manual_or_emergency",
    "cloud_to_local_after_minutes": 60,
    "next_boot_livekit": "auto",
    "cloud_to_local_legacy": True,
    "gemini_standby_enabled": False,
    "gemini_standby_mode": "dry_run",
    "allow_real_gemini_standby": False,
    "gemini_standby_after_minutes": 60,
    "wake_text_enabled": True,
    "wake_audio_enabled": False,
    "gemini_standby_rollback_on_failure": True,
    "wake_only_enabled": True,
    "wake_words": [
        "kira",
        "akira",
        "oi kira",
        "e ai kira",
        "kira ta ai",
        "kira me escuta",
        "kira cheguei",
        "kira volta",
        "kira preciso de voce",
        "oi",
        "ola",
        "ei",
        "hey",
        "acorda",
        "ta ai",
        "ta por ai",
        "preciso de voce",
        "escuta",
        "me escuta",
        "me ajuda",
        "ajuda",
        "bom dia",
        "boa tarde",
        "boa noite",
        "cheguei",
        "voltei",
        "alo",
        "psiu",
        "fala comigo",
        "vem ca",
        "to aqui",
        "acorda kira",
        "oi amor",
        "e ai",
    ],
    "exit_wake_only_after_user_messages": 3,
    "return_to_economy_after_minutes": 60,
    "active_after_wake_grace_seconds": 180,
    "gemini_preventive_refresh_enabled": False,
    "gemini_refresh_after_wake_cycles": 4,
    "gemini_preventive_rotation_enabled": False,
    "gemini_preventive_rotation_after_minutes": 30,
    "gemini_preventive_rotation_warmup_window_minutes": 10,
    "gemini_preventive_rotation_retry_after_minutes": 5,
    "gemini_preventive_rotation_max_warmup_attempts": 2,
    "gemini_preventive_rotation_require_audio_probe": True,
    "standby_wake_ack_cache_enabled": False,
    "standby_wake_ack_cache_ttl_minutes": 120,
    "standby_wake_ack_generate_before_standby": True,
    "standby_wake_ack_play_on_wake": True,
    "prefer_livekit_local_in_economy": False,
    "fallback_to_local_if_cloud_fails": False,
    "do_not_enter_if_speaking": True,
    "do_not_enter_if_tool_running": True,
    "do_not_enter_if_manus_running": True,
    "do_not_enter_if_pc_operator_running": True,
    "do_not_enter_if_messages_sending": True,
    "do_not_enter_if_remote_client_connected": False,
    "health_check_interval_seconds": 30,
    "log_interval_seconds": 60,
    "enable_real_cloud_to_local_handoff": False,
    "handoff_cooldown_seconds": 300,
    "handoff_timeout_seconds": 90,
    "rollback_on_handoff_failure": True,
    "manual_handoff_enabled": True,
    "auto_handoff_enabled": False,
    "manual_switch_requires_ui_reconnect": True,
}


def _deep_merge(defaults: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in dict(raw or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "sim", "on"}:
        return True
    if text in {"0", "false", "no", "nao", "não", "off"}:
        return False
    return default


def _coerce_int(value: Any, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        coerced = int(value)
    except Exception:
        coerced = default
    coerced = max(coerced, minimum)
    if maximum is not None:
        coerced = min(coerced, maximum)
    return coerced


def normalize_wake_words(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = DEFAULT_ECONOMY_MODE_CONFIG["wake_words"]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").strip().lower().split())
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized or list(DEFAULT_ECONOMY_MODE_CONFIG["wake_words"])


def normalize_economy_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = _deep_merge(DEFAULT_ECONOMY_MODE_CONFIG, raw or {})
    bool_keys = [
        "enabled",
        "livekit_local_default",
        "livekit_cloud_disabled_by_default",
        "livekit_cloud_fallback_enabled",
        "cloud_to_local_legacy",
        "gemini_standby_enabled",
        "allow_real_gemini_standby",
        "wake_text_enabled",
        "wake_audio_enabled",
        "gemini_standby_rollback_on_failure",
        "wake_only_enabled",
        "prefer_livekit_local_in_economy",
        "fallback_to_local_if_cloud_fails",
        "do_not_enter_if_speaking",
        "do_not_enter_if_tool_running",
        "do_not_enter_if_manus_running",
        "do_not_enter_if_pc_operator_running",
        "do_not_enter_if_messages_sending",
        "do_not_enter_if_remote_client_connected",
        "enable_real_cloud_to_local_handoff",
        "rollback_on_handoff_failure",
        "manual_handoff_enabled",
        "auto_handoff_enabled",
        "manual_switch_requires_ui_reconnect",
        "gemini_preventive_refresh_enabled",
        "gemini_preventive_rotation_enabled",
        "gemini_preventive_rotation_require_audio_probe",
        "standby_wake_ack_cache_enabled",
        "standby_wake_ack_generate_before_standby",
        "standby_wake_ack_play_on_wake",
    ]
    for key in bool_keys:
        merged[key] = _coerce_bool(merged.get(key), bool(DEFAULT_ECONOMY_MODE_CONFIG[key]))

    mode = str(merged.get("mode") or DEFAULT_ECONOMY_MODE_CONFIG["mode"]).strip().lower()
    merged["mode"] = mode if mode in {"gemini_only", "legacy"} else DEFAULT_ECONOMY_MODE_CONFIG["mode"]
    fallback_mode = str(merged.get("cloud_fallback_mode") or DEFAULT_ECONOMY_MODE_CONFIG["cloud_fallback_mode"]).strip().lower()
    merged["cloud_fallback_mode"] = fallback_mode if fallback_mode in {"manual_or_emergency", "manual_only", "disabled"} else DEFAULT_ECONOMY_MODE_CONFIG["cloud_fallback_mode"]
    standby_mode = str(merged.get("gemini_standby_mode") or DEFAULT_ECONOMY_MODE_CONFIG["gemini_standby_mode"]).strip().lower()
    merged["gemini_standby_mode"] = standby_mode if standby_mode in {"dry_run", "real"} else DEFAULT_ECONOMY_MODE_CONFIG["gemini_standby_mode"]

    minute_keys = [
        "cloud_to_local_after_minutes",
        "gemini_standby_after_minutes",
        "exit_wake_only_after_user_messages",
        "return_to_economy_after_minutes",
        "gemini_preventive_rotation_after_minutes",
        "gemini_preventive_rotation_warmup_window_minutes",
        "gemini_preventive_rotation_retry_after_minutes",
        "gemini_preventive_rotation_max_warmup_attempts",
        "standby_wake_ack_cache_ttl_minutes",
    ]
    for key in minute_keys:
        merged[key] = _coerce_int(merged.get(key), int(DEFAULT_ECONOMY_MODE_CONFIG[key]), minimum=1, maximum=24 * 60)

    merged["active_after_wake_grace_seconds"] = _coerce_int(
        merged.get("active_after_wake_grace_seconds"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["active_after_wake_grace_seconds"]),
        minimum=0,
        maximum=24 * 3600,
    )
    merged["gemini_refresh_after_wake_cycles"] = _coerce_int(
        merged.get("gemini_refresh_after_wake_cycles"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["gemini_refresh_after_wake_cycles"]),
        minimum=1,
        maximum=100,
    )

    merged["health_check_interval_seconds"] = _coerce_int(
        merged.get("health_check_interval_seconds"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["health_check_interval_seconds"]),
        minimum=10,
        maximum=3600,
    )
    merged["log_interval_seconds"] = _coerce_int(
        merged.get("log_interval_seconds"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["log_interval_seconds"]),
        minimum=10,
        maximum=3600,
    )
    merged["handoff_cooldown_seconds"] = _coerce_int(
        merged.get("handoff_cooldown_seconds"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["handoff_cooldown_seconds"]),
        minimum=30,
        maximum=24 * 3600,
    )
    merged["handoff_timeout_seconds"] = _coerce_int(
        merged.get("handoff_timeout_seconds"),
        int(DEFAULT_ECONOMY_MODE_CONFIG["handoff_timeout_seconds"]),
        minimum=10,
        maximum=600,
    )
    boot = str(merged.get("next_boot_livekit") or "auto").strip().lower()
    merged["next_boot_livekit"] = boot if boot in {"auto", "local", "cloud"} else "auto"
    merged["wake_words"] = normalize_wake_words(merged.get("wake_words"))
    return merged


def load_economy_mode_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    cache_key = str(config_path)

    # Fast path: cache valido pelo mtime -> sem I/O de disco nem re-normalizacao.
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None:
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_CACHE.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return deepcopy(cached[1])

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config = normalize_economy_config(DEFAULT_ECONOMY_MODE_CONFIG)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("ECONOMY_CONFIG_LOADED path=%s created=true", config_path)
        _store_config_cache(config_path, config)
        return deepcopy(config)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except Exception as exc:
        logger.warning("ECONOMY_CONFIG_LOAD_FAILED path=%s error=%s", config_path, exc)
        raw = {}
    config = normalize_economy_config(raw)
    if config != raw:
        # Migracao unica: grava a versao normalizada. Como normalize eh idempotente,
        # a proxima leitura tera raw == config e nao reescrevera mais.
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("ECONOMY_CONFIG_LOADED path=%s created=false", config_path)
    _store_config_cache(config_path, config)
    return deepcopy(config)


def _store_config_cache(config_path: Path, config: dict[str, Any]) -> None:
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[str(config_path)] = (mtime, deepcopy(config))


def save_economy_mode_config(config: dict[str, Any], path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_economy_config(config)
    config_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("ECONOMY_CONFIG_UPDATED path=%s", config_path)
    # Atualiza o cache para que leitores subsequentes vejam a config nova sem
    # depender do timing do mtime.
    _store_config_cache(config_path, normalized)
    return deepcopy(normalized)
