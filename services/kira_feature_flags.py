from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.feature_flags")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "kira_features.json"

OPTIONAL_MODULES = (
    "messages",
    "manus",
    "internet",
    "browser",
    "system_vitals",
    "pc_operator",
    "music",
    "vision",
    "panels",
    "diagnostics",
    "whatsapp",
)

DEFAULT_FEATURES: dict[str, Any] = {
    "core_mode": True,
    "optional_modules": {
        name: {
            "enabled": False,
            "autoload": False,
            "show_when_disabled": False,
        }
        for name in OPTIONAL_MODULES
    },
}


def _deep_merge(defaults: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in dict(raw or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_kira_features() -> dict[str, Any]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_FEATURES, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("KIRA_CORE_MODE_ENABLED config_created=%s", CONFIG_PATH)
        return deepcopy(DEFAULT_FEATURES)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("KIRA_FEATURE_CONFIG_READ_FAILED error=%s", exc)
        return deepcopy(DEFAULT_FEATURES)
    cfg = _deep_merge(DEFAULT_FEATURES, raw if isinstance(raw, dict) else {})
    changed = cfg != raw
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if bool(cfg.get("core_mode", True)):
        logger.info("KIRA_CORE_MODE_ENABLED")
    return cfg


def get_optional_modules() -> dict[str, dict[str, bool]]:
    cfg = load_kira_features()
    modules = cfg.get("optional_modules") or {}
    return {
        name: {
            "enabled": bool((modules.get(name) or {}).get("enabled", False)),
            "autoload": bool((modules.get(name) or {}).get("autoload", False)),
            "show_when_disabled": bool((modules.get(name) or {}).get("show_when_disabled", False)),
        }
        for name in OPTIONAL_MODULES
    }


def is_optional_enabled(module: str) -> bool:
    return bool(get_optional_modules().get(str(module or "").strip(), {}).get("enabled", False))


def should_autoload(module: str) -> bool:
    item = get_optional_modules().get(str(module or "").strip(), {})
    return bool(item.get("enabled") and item.get("autoload"))


def should_show_when_disabled(module: str) -> bool:
    return bool(get_optional_modules().get(str(module or "").strip(), {}).get("show_when_disabled", False))


def optional_status_payload(module: str) -> dict[str, Any]:
    name = str(module or "").strip()
    item = get_optional_modules().get(name, {"enabled": False, "autoload": False, "show_when_disabled": False})
    return {
        "module": name,
        "enabled": bool(item.get("enabled")),
        "autoload": bool(item.get("autoload")),
        "show_when_disabled": bool(item.get("show_when_disabled")),
        "status": "disabled" if not item.get("enabled") else "available",
        "critical": False,
    }
