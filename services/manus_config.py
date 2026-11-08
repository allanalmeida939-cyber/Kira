from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _read_env_from_root() -> dict[str, str]:
    data: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return data
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


@dataclass
class ManusApiKeySlot:
    slot: int
    env_name: str
    key_present: bool
    key_value: str | None
    enabled: bool


@dataclass
class ManusConfig:
    enabled: bool
    base_url: str
    api_keys: list[ManusApiKeySlot]
    default_agent_profile: str | None
    output_root: Path
    task_timeout_minutes: int
    poll_interval_seconds: int
    max_retries: int
    key_strategy: str
    log_http: bool
    api_mode: str
    allow_local_artifact_fallback: bool = False
    retry_auth_failed_keys: bool = False
    reset_key_state_on_start: bool = True

    @property
    def agent_profile(self) -> str:
        return str(self.default_agent_profile or "manus-1.6-lite")

    def get_task_create_endpoint(self) -> str:
        """Retorna o endpoint correto sem duplicação."""
        base = str(self.base_url or "https://api.manus.ai").strip().rstrip("/")
        base = base.removesuffix("/v2/task.create").removesuffix("/v1/tasks").rstrip("/")
        if str(self.api_mode or "v2_task_create").strip().lower() != "v2_task_create":
            logger.warning("MANUS_CONFIG_UNSUPPORTED_API_MODE mode=%s", self.api_mode)
        return f"{base}/v2/task.create"


def _env_bool(name: str, default: bool) -> bool:
    v = str(os.getenv(name, str(default))).strip().lower()
    return v in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def load_manus_config() -> ManusConfig:
    logger.info("MANUS_CONFIG_LOAD_BEGIN")
    root_env = _read_env_from_root()
    logger.info("MANUS_ENV_PATH path=%s", str(_ENV_PATH))
    def _env(name: str, default: str = "") -> str:
        if name in root_env:
            return str(root_env.get(name, default))
        return str(os.getenv(name, default))
    def _env_bool_local(name: str, default: bool) -> bool:
        return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}
    def _env_int_local(name: str, default: int) -> int:
        try:
            return int(_env(name, str(default)).strip())
        except Exception:
            return default
    enabled = _env_bool_local("MANUS_ENABLED", False)
    base_url = (_env("MANUS_BASE_URL", "https://api.manus.ai").strip() or "https://api.manus.ai").rstrip("/")
    api_mode = _env("MANUS_API_MODE", "v2_task_create").strip() or "v2_task_create"
    default_agent_profile = _env("MANUS_DEFAULT_AGENT_PROFILE", "manus-1.6-lite").strip() or "manus-1.6-lite"
    output_root_raw = _env("MANUS_OUTPUT_ROOT", "").strip()
    output_root = Path(output_root_raw) if output_root_raw else (Path.home() / "Documents" / "Kira")
    api_keys: list[ManusApiKeySlot] = []
    present_slots: list[int] = []
    legacy_api_key = _env("MANUS_API_KEY", "").strip()
    for slot in range(1, 6):
        env_name = f"MANUS_API_KEY_{slot}"
        value = _env(env_name, "").strip()
        if slot == 1 and not value and legacy_api_key:
            value = legacy_api_key
        present = bool(value)
        if present:
            present_slots.append(slot)
        api_keys.append(ManusApiKeySlot(slot=slot, env_name=env_name, key_present=present, key_value=value or None, enabled=present))

    cfg = ManusConfig(
        enabled=enabled,
        base_url=base_url,
        api_keys=api_keys,
        default_agent_profile=default_agent_profile,
        output_root=output_root,
        task_timeout_minutes=max(1, _env_int_local("MANUS_TASK_TIMEOUT_MINUTES", 45)),
        poll_interval_seconds=max(1, _env_int_local("MANUS_POLL_INTERVAL_SECONDS", 8)),
        max_retries=max(0, _env_int_local("MANUS_MAX_RETRIES", 5)),
        key_strategy=_env("MANUS_KEY_STRATEGY", "fallback").strip() or "fallback",
        log_http=_env_bool_local("MANUS_LOG_HTTP", False),
        api_mode=api_mode,
        allow_local_artifact_fallback=_env_bool_local("KIRA_ALLOW_LOCAL_ARTIFACT_FALLBACK", False),
        retry_auth_failed_keys=_env_bool_local("MANUS_RETRY_AUTH_FAILED_KEYS", False),
        reset_key_state_on_start=_env_bool_local("MANUS_RESET_KEY_STATE_ON_START", True),
    )
    logger.info("MANUS_CONFIG_OUTPUT_ROOT path=%s", str(cfg.output_root))
    logger.info(
        "MANUS_CONFIG_SAFE_SUMMARY enabled=%s base_url_present=%s api_mode=%s key_slots_present=%s profile=%s",
        str(cfg.enabled).lower(),
        str(bool(cfg.base_url)).lower(),
        cfg.api_mode,
        present_slots,
        cfg.agent_profile,
    )
    if not is_manus_configured(cfg):
        logger.info("MANUS_CONFIG_DISABLED reason=no_keys_or_disabled")
    return cfg


def get_available_key_slots(config: ManusConfig) -> list[ManusApiKeySlot]:
    return [s for s in config.api_keys if s.enabled and s.key_present and bool(s.key_value)]


def is_manus_configured(config: ManusConfig) -> bool:
    return bool(config.enabled and get_available_key_slots(config))
