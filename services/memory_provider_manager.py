from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable


logger = logging.getLogger("kira.memory_provider")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_MEMORY_PROVIDERS = {"auto", "mem0", "local", "none"}
MAX_LOCAL_MEMORY_BYTES = 512 * 1024


@dataclass(frozen=True)
class MemoryProviderState:
    provider: str
    available: bool
    reason: str | None = None
    loaded_at: float | None = None


_state_lock = RLock()
_selected_state: MemoryProviderState | None = None


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _configured_mode() -> str:
    mode = str(os.getenv("KIRA_MEMORY_PROVIDER", "mem0") or "mem0").strip().lower()
    if mode not in VALID_MEMORY_PROVIDERS:
        logger.warning("MEMORY_PROVIDER_INVALID mode=%s fallback=auto", mode)
        return "auto"
    return mode


def _load_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("KIRA_MEMORY_LOAD_TIMEOUT", "3") or "3"), 0.1)
    except Exception:
        return 3.0


def _local_memory_file() -> Path:
    configured = str(os.getenv("KIRA_LOCAL_MEMORY_FILE", "configs/memory/kira_memory.json") or "").strip()
    path = Path(configured or "configs/memory/kira_memory.json").expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _mem0_configured() -> bool:
    try:
        from services.mem0_cloud_provider import resolve_mem0_api_key
    except Exception:
        return bool(str(os.getenv("MEM0_API_KEY", "") or "").strip())
    return resolve_mem0_api_key() is not None


def _mem0_available() -> tuple[bool, str]:
    if not _mem0_configured():
        logger.info("MEMORY_MEM0_CONFIG_MISSING")
        return False, "mem0_api_key_missing"
    try:
        from services.mem0_cloud_provider import Mem0CloudProvider

        provider = Mem0CloudProvider()
        provider.get_client()
        provider.close()
    except Exception as exc:
        return False, f"mem0_import_failed:{exc.__class__.__name__}"
    return True, ""


def _local_available() -> tuple[bool, str]:
    path = _local_memory_file()
    try:
        if path.exists() and path.is_file() and path.stat().st_size > MAX_LOCAL_MEMORY_BYTES:
            return False, "local_memory_file_too_large"
        if path.exists() and path.is_file():
            with path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            if not isinstance(payload, dict):
                return False, "local_memory_json_not_object"
        return True, ""
    except Exception as exc:
        logger.warning("MEMORY_PROVIDER_LOCAL_FAILED_CONTINUING reason=%s", exc)
        return False, f"local_memory_failed:{exc.__class__.__name__}"


def _select_state(provider: str, *, available: bool, reason: str | None = None) -> MemoryProviderState:
    state = MemoryProviderState(
        provider=provider,
        available=available,
        reason=reason,
        loaded_at=time.time(),
    )
    logger.info("MEMORY_PROVIDER_SELECTED provider=%s source=env reason=%s", provider, reason or "")
    if provider == "mem0" and available:
        try:
            from services.mem0_cloud_provider import Mem0CloudProvider

            mem0_provider = Mem0CloudProvider()
            logger.info(
                "MEM0_PROVIDER_READY user_id=%s agent_id=%s app_id=%s",
                mem0_provider.user_id,
                mem0_provider.agent_id,
                mem0_provider.app_id,
            )
        except Exception as exc:
            logger.warning("MEM0_PROVIDER_READY_STATUS_FAILED reason=%s", exc)
    return state


def select_memory_provider(*, force_refresh: bool = False) -> MemoryProviderState:
    global _selected_state
    with _state_lock:
        if _selected_state is not None and not force_refresh:
            return _selected_state
        mode = _configured_mode()
        logger.info("MEMORY_PROVIDER_SELECT_BEGIN mode=%s", mode)

        if mode == "none":
            _selected_state = _select_state("none", available=False, reason="disabled_by_config")
            return _selected_state

        if mode in {"auto", "mem0"}:
            ok, reason = _mem0_available()
            if ok:
                _selected_state = _select_state("mem0", available=True)
                return _selected_state
            logger.warning("MEM0_PROVIDER_UNAVAILABLE reason=%s", reason)
            if mode == "mem0":
                _selected_state = _select_state("none", available=False, reason=reason)
                return _selected_state

        if mode in {"auto", "local"}:
            ok, reason = _local_available()
            if ok:
                if mode == "auto":
                    logger.warning("MEMORY_PROVIDER_FALLBACK_USED provider=local reason=mem0_unavailable")
                _selected_state = _select_state("local", available=True, reason=reason or None)
                return _selected_state
            logger.warning("MEMORY_PROVIDER_LOCAL_FAILED_CONTINUING reason=%s", reason)
            _selected_state = _select_state("none", available=False, reason=reason)
            return _selected_state

        _selected_state = _select_state("none", available=False, reason="no_provider_available")
        return _selected_state


def get_selected_memory_provider() -> MemoryProviderState:
    return select_memory_provider()


def is_memory_enabled() -> bool:
    return get_selected_memory_provider().provider != "none"


def _run_with_timeout(fn: Callable[[], Any], *, timeout_seconds: float) -> Any:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kira-memory")
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _empty_context(provider: str, reason: str) -> dict[str, Any]:
    if provider == "none":
        logger.info("MEMORY_CONTEXT_EMPTY_CONTINUING provider=none")
    return {
        "ok": True,
        "provider": provider,
        "source": provider,
        "items": [],
        "text": "",
        "empty": True,
        "reason": reason,
    }


def _load_mem0_context(*, max_items: int, max_chars: int, user_id: str) -> dict[str, Any]:
    logger.info("MEMORY_MEM0_LOAD_BEGIN")

    def _load() -> Any:
        from services.mem0_cloud_provider import Mem0CloudProvider

        provider = Mem0CloudProvider()
        try:
            return provider.get_all(user_id=user_id, limit=max_items)
        finally:
            provider.close()

    try:
        response = _run_with_timeout(_load, timeout_seconds=_load_timeout_seconds())
    except FutureTimeoutError as exc:
        logger.warning("MEMORY_MEM0_LOAD_FAILED_CONTINUING reason=timeout")
        raise RuntimeError("mem0_timeout") from exc
    except Exception as exc:
        logger.warning("MEMORY_MEM0_LOAD_FAILED_CONTINUING reason=%s", exc)
        raise

    results = response if isinstance(response, list) else []
    lines: list[str] = []
    for item in results[:max_items]:
        if isinstance(item, dict):
            text = str(item.get("memory") or item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            lines.append(f"- {text}")
    text = "\n".join(lines).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    logger.info("MEMORY_MEM0_LOAD_OK items=%s", len(lines))
    if text:
        logger.info("MEMORY_CONTEXT_OPTIONAL_LOADED provider=mem0")
        return {"ok": True, "provider": "mem0", "source": "mem0", "items": lines, "text": text, "empty": False, "reason": ""}
    return _empty_context("mem0", "mem0_empty")


def _load_local_payload() -> dict[str, Any]:
    path = _local_memory_file()
    if not path.exists():
        logger.info("MEMORY_LOCAL_EMPTY")
        return {"events": [], "summary": ""}
    if path.stat().st_size > MAX_LOCAL_MEMORY_BYTES:
        raise RuntimeError("local_memory_file_too_large")
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict):
        raise RuntimeError("local_memory_json_not_object")
    payload.setdefault("events", [])
    payload.setdefault("summary", "")
    return payload


def _load_local_context(*, max_items: int, max_chars: int) -> dict[str, Any]:
    logger.info("MEMORY_LOCAL_LOAD_BEGIN")
    try:
        payload = _load_local_payload()
    except Exception as exc:
        logger.warning("MEMORY_LOCAL_LOAD_FAILED_CONTINUING reason=%s", exc)
        raise
    lines: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"- Resumo: {summary}")
    events = payload.get("events")
    if isinstance(events, list):
        for event in events[-max_items:]:
            if not isinstance(event, dict):
                continue
            role = str(event.get("role") or "memoria").strip()
            text = str(event.get("text") or event.get("content") or "").strip()
            if text:
                lines.append(f"- {role}: {text}")
    lines = lines[:max_items]
    text = "\n".join(lines).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    logger.info("MEMORY_LOCAL_LOAD_OK items=%s", len(lines))
    if text:
        logger.info("MEMORY_CONTEXT_OPTIONAL_LOADED provider=local")
        return {"ok": True, "provider": "local", "source": "local", "items": lines, "text": text, "empty": False, "reason": ""}
    return _empty_context("local", "local_empty")


def load_memory_context(*, max_items: int = 5, max_chars: int = 1200, user_id: str | None = None) -> dict[str, Any]:
    global _selected_state
    state = select_memory_provider()
    if state.provider == "none":
        return _empty_context("none", state.reason or "provider_none")
    if state.provider == "mem0":
        try:
            return _load_mem0_context(
                max_items=max(0, int(max_items)),
                max_chars=max(0, int(max_chars)),
                user_id=str(user_id or os.getenv("MEM0_USER_ID", "kira_user") or "kira_user"),
            )
        except Exception as exc:
            if _configured_mode() == "auto":
                with _state_lock:
                    _selected_state = None
                local_ok, local_reason = _local_available()
                if local_ok:
                    with _state_lock:
                        _selected_state = _select_state("local", available=True)
                    return _load_local_context(max_items=max_items, max_chars=max_chars)
                with _state_lock:
                    _selected_state = _select_state("none", available=False, reason=local_reason or str(exc))
            return _empty_context("none", str(exc))
    if state.provider == "local":
        try:
            return _load_local_context(max_items=max(0, int(max_items)), max_chars=max(0, int(max_chars)))
        except Exception as exc:
            with _state_lock:
                _selected_state = _select_state("none", available=False, reason=str(exc))
            return _empty_context("none", str(exc))
    return _empty_context("none", "unknown_provider")


def save_memory_event(*, role: str, text: str, metadata: dict[str, Any] | None = None, user_id: str | None = None) -> dict[str, Any]:
    if not _env_flag("KIRA_MEMORY_SAVE_ENABLED", default=True):
        logger.info("MEMORY_SAVE_SKIPPED provider=none reason=disabled")
        return {"ok": True, "saved": False, "provider": "none", "reason": "disabled"}
    state = select_memory_provider()
    logger.info("MEMORY_SAVE_BEGIN provider=%s", state.provider)
    clean_text = str(text or "").strip()
    if not clean_text or state.provider == "none":
        logger.info("MEMORY_SAVE_SKIPPED provider=%s", state.provider)
        return {"ok": True, "saved": False, "provider": state.provider, "reason": "empty_or_none"}
    try:
        if state.provider == "mem0":
            try:
                from services.sensitive_vault import SensitiveVault, classify_sensitive_text, third_party_sensitive_mem0_allowed

                classification = classify_sensitive_text(clean_text)
                if classification.sensitive and classification.owner == "alan":
                    logger.info("SENSITIVE_VAULT_ROUTE owner=alan category=%s action=local_vault", classification.category)
                    logger.info("MEM0_SAVE_BLOCKED_PERSONAL_SENSITIVE reason=alan_sensitive")
                    try:
                        from services.sensitive_vault import _extract_value

                        raw_value = _extract_value(clean_text)
                    except Exception:
                        raw_value = clean_text
                    SensitiveVault().save(
                        raw_value=raw_value,
                        category=classification.category,
                        label=classification.label,
                        context=clean_text,
                        source=str((metadata or {}).get("source") or "memory_event"),
                    )
                    return {"ok": True, "saved": True, "provider": "sensitive_vault", "reason": "alan_sensitive"}
                if classification.sensitive and classification.owner == "third_party" and not third_party_sensitive_mem0_allowed():
                    logger.info("SENSITIVE_THIRD_PARTY_POLICY action=blocked_or_confirm_required")
                    return {"ok": True, "saved": False, "provider": "mem0", "reason": "third_party_sensitive_blocked"}
            except Exception as exc:
                logger.warning("SENSITIVE_VAULT_ERROR error_type=%s", exc.__class__.__name__)
            logger.info("MEMORY_MEM0_SAVE_BEGIN")

            def _save() -> Any:
                from services.mem0_cloud_provider import Mem0CloudProvider

                provider = Mem0CloudProvider()
                try:
                    return provider.add_text(
                        clean_text,
                        user_id=str(user_id or os.getenv("MEM0_USER_ID", "kira_user") or "kira_user"),
                        metadata=dict(metadata or {}),
                    )
                finally:
                    provider.close()

            result = _run_with_timeout(_save, timeout_seconds=_load_timeout_seconds())
            if isinstance(result, dict) and result.get("blocked_sensitive"):
                logger.info("MEMORY_MEM0_SAVE_SKIPPED reason=blocked_sensitive")
                return {"ok": True, "saved": False, "provider": "mem0", "reason": "blocked_sensitive"}
            if isinstance(result, dict) and not result.get("ok", True):
                raise RuntimeError(str(result.get("safe_message") or result.get("reason") or "mem0_save_failed"))
            logger.info("MEMORY_MEM0_SAVE_OK")
            logger.info("MEMORY_SAVE_OK provider=mem0")
            return {"ok": True, "saved": True, "provider": "mem0"}
        if state.provider == "local":
            payload = _load_local_payload()
            events = payload.get("events")
            if not isinstance(events, list):
                events = []
            events.append({
                "timestamp": time.time(),
                "role": str(role or "memory"),
                "text": clean_text,
                "metadata": dict(metadata or {}),
            })
            payload["events"] = events[-200:]
            path = _local_memory_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            tmp.replace(path)
            logger.info("MEMORY_LOCAL_SAVE_OK")
            logger.info("MEMORY_SAVE_OK provider=local")
            return {"ok": True, "saved": True, "provider": "local"}
    except Exception as exc:
        if state.provider == "mem0":
            logger.warning("MEMORY_MEM0_SAVE_FAILED_NON_FATAL reason=%s", exc)
        if state.provider == "local":
            logger.warning("MEMORY_LOCAL_SAVE_FAILED_NON_FATAL reason=%s", exc)
        logger.warning("MEMORY_SAVE_FAILED_NON_FATAL provider=%s reason=%s", state.provider, exc)
        return {"ok": False, "saved": False, "provider": state.provider, "reason": str(exc)}
    logger.info("MEMORY_SAVE_SKIPPED provider=%s", state.provider)
    return {"ok": True, "saved": False, "provider": state.provider, "reason": "unsupported_provider"}


def get_memory_status() -> dict[str, Any]:
    state = get_selected_memory_provider()
    data = asdict(state)
    try:
        from services.mem0_cloud_provider import Mem0CloudProvider

        provider = Mem0CloudProvider()
        data["user_id"] = provider.user_id
        data["agent_id"] = provider.agent_id
        data["app_id"] = provider.app_id
        data["local_memory_active"] = state.provider == "local"
        try:
            from services.sensitive_vault import get_sensitive_vault_status

            vault_status = get_sensitive_vault_status()
        except Exception:
            vault_status = {"active": False, "path_exists": False, "items_count": 0}
        data["sensitive_vault_active"] = bool(vault_status.get("active"))
        data["sensitive_vault_path_exists"] = bool(vault_status.get("path_exists"))
        data["sensitive_vault_items_count"] = int(vault_status.get("items_count") or 0)
        data["sensitive_vault_crypto_backend"] = str(vault_status.get("crypto_backend") or "")
        data["last_error"] = None if state.available else state.reason
    except Exception as exc:
        data["local_memory_active"] = state.provider == "local"
        data["sensitive_vault_active"] = False
        data["sensitive_vault_path_exists"] = False
        data["sensitive_vault_items_count"] = 0
        data["last_error"] = str(exc)
    data["status_visible"] = _env_flag("KIRA_MEMORY_STATUS_VISIBLE", default=True)
    data["save_enabled"] = _env_flag("KIRA_MEMORY_SAVE_ENABLED", default=True)
    data["mode"] = _configured_mode()
    return data


def get_memory_provider_status() -> dict[str, Any]:
    return get_memory_status()
