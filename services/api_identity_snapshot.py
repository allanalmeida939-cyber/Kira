from __future__ import annotations

import logging
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from services.livekit_provider_manager import (
    DEFAULT_STATE_FILE,
    LiveKitProvider,
    build_provider_candidates,
    get_selected_provider,
)


logger = logging.getLogger("kira.app")

EMAIL_NOT_INFORMED = "e-mail não informado"
_PLACEHOLDER_EMAIL_PARTS = (
    "colocar_email",
    "email_",
    "exemplo",
    "example",
    "nao_informado",
    "não_informado",
)


@dataclass
class ApiIdentitySnapshotStore:
    snapshot: dict[str, Any] | None = None
    created_at: float = 0.0
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


_store = ApiIdentitySnapshotStore()


def _env(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else _store.env
    return str(source.get(name, default) or "").strip()


def _is_configured(value: str) -> bool:
    return bool(str(value or "").strip())


def _is_real_email(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(part in lowered for part in _PLACEHOLDER_EMAIL_PARTS):
        return False
    return "@" in text and "." in text


def _email_or_placeholder(*names: str, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    for name in names:
        value = _env(name, env=env)
        if _is_real_email(value):
            return value, True
    return EMAIL_NOT_INFORMED, False


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def _compact_key_preview(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "..." in text:
        left, right = text.split("...", 1)
        return f"{left[:4]}...{right[-4:]}"
    return _mask_secret(text)


def _mask_livekit_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        host = parsed.netloc
        if not host:
            return ""
        parts = host.split(".")
        if parts:
            first = parts[0]
            prefix = first.split("-", 1)[0] if "-" in first else first[:4]
            parts[0] = f"{prefix}-***" if prefix else "***"
        return f"{parsed.scheme}://{'.'.join(parts)}"
    except Exception:
        return ""


def _slot_from_provider_name(name: str) -> int | None:
    match = re.fullmatch(r"cloud_(\d+)", str(name or "").strip().lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _livekit_total_slots(env: Mapping[str, str]) -> int:
    configured = 0
    for slot in range(1, 4):
        if (
            _env(f"LIVEKIT_CLOUD_URL_{slot}", env=env)
            and _env(f"LIVEKIT_CLOUD_API_KEY_{slot}", env=env)
            and _env(f"LIVEKIT_CLOUD_API_SECRET_{slot}", env=env)
        ):
            configured += 1
    return configured


def _resolve_livekit_provider(env: Mapping[str, str], provider: LiveKitProvider | None) -> LiveKitProvider | None:
    if provider is not None:
        return provider
    selected = get_selected_provider()
    if selected is not None:
        return selected
    selected_name = _env("LIVEKIT_PROVIDER_SELECTED", env=env) or _env("LIVEKIT_PROVIDER_NAME", env=env)
    if selected_name:
        for candidate in build_provider_candidates(env):
            if candidate.name == selected_name:
                return candidate
    try:
        if DEFAULT_STATE_FILE.exists():
            state = json.loads(DEFAULT_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state_provider = str(state.get("last_success_provider") or "").strip()
                for candidate in build_provider_candidates(env):
                    if candidate.name == state_provider:
                        return candidate
    except Exception:
        pass
    for candidate in build_provider_candidates(env):
        if candidate.kind == "cloud" and candidate.url == _env("LIVEKIT_URL", env=env):
            return candidate
    return None


def _build_livekit_identity(env: Mapping[str, str], provider: LiveKitProvider | None) -> dict[str, Any]:
    selected = _resolve_livekit_provider(env, provider)
    total_slots = _livekit_total_slots(env)
    if selected is None:
        return {
            "slot": None,
            "active_slot": None,
            "total": total_slots,
            "total_slots": total_slots,
            "provider": "",
            "provider_name": "",
            "kind": "",
            "email": EMAIL_NOT_INFORMED,
            "email_present": False,
            "url_masked": "",
            "key_preview": "",
            "status": "offline",
        }

    if selected.kind == "local":
        email, present = _email_or_placeholder("LIVEKIT_LOCAL_EMAIL", env=env)
        return {
            "slot": None,
            "active_slot": None,
            "total": total_slots,
            "total_slots": total_slots,
            "provider": selected.name,
            "provider_name": selected.name,
            "kind": "local",
            "email": email,
            "email_present": present,
            "url_masked": _mask_livekit_url(selected.url),
            "key_preview": _mask_secret(selected.api_key),
            "status": "online",
        }

    slot = _slot_from_provider_name(selected.name)
    email, present = _email_or_placeholder(
        f"LIVEKIT_CLOUD_{slot}_EMAIL" if slot else "",
        f"LIVEKIT_CLOUD_EMAIL_{slot}" if slot else "",
        env=env,
    )
    return {
        "slot": slot,
        "active_slot": slot,
        "total": total_slots,
        "total_slots": total_slots,
        "provider": selected.name,
        "provider_name": selected.name,
        "kind": "cloud",
        "email": email,
        "email_present": present,
        "url_masked": _mask_livekit_url(selected.url),
        "key_preview": _mask_secret(selected.api_key),
        "status": "online",
    }


def _fallback_gemini_status_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    keys = []
    for slot in range(1, 5):
        key = _env(f"GEMINI_{slot}_API_KEY", env=env)
        if slot == 1 and not key:
            key = _env("GOOGLE_API_KEY", env=env)
        if key:
            keys.append((slot, key))
    slot, key = keys[0] if keys else (None, "")
    return {
        "current_text_index": (slot - 1) if slot else None,
        "gemini_current_slot": slot,
        "gemini_total_slots": len(keys),
        "current_text_key_preview": _mask_secret(key),
    }


def _load_gemini_status(env: Mapping[str, str], key_report: Mapping[str, Any] | None) -> dict[str, Any]:
    if key_report:
        return dict(key_report)
    try:
        from agent.gemini_key_manager import get_key_manager

        return dict(get_key_manager().get_status_report())
    except Exception as exc:
        logger.warning("API_IDENTITY_GEMINI_STATUS_FALLBACK reason=%s", exc.__class__.__name__)
        return _fallback_gemini_status_from_env(env)


def _build_gemini_identity(env: Mapping[str, str], key_report: Mapping[str, Any] | None) -> dict[str, Any]:
    status = _load_gemini_status(env, key_report)
    slot = status.get("gemini_current_slot")
    if slot is None and status.get("current_text_index") is not None:
        try:
            slot = int(status.get("current_text_index")) + 1
        except Exception:
            slot = None
    total = int(status.get("gemini_total_slots") or status.get("usable_keys") or status.get("configured_keys") or 0)
    slot_int = int(slot) if slot is not None else None
    email, present = _email_or_placeholder(
        f"GEMINI_API_KEY_{slot_int}_EMAIL" if slot_int else "",
        f"GEMINI_{slot_int}_API_KEY_EMAIL" if slot_int else "",
        env=env,
    )
    return {
        "slot": slot_int,
        "active_slot": slot_int,
        "total": total,
        "total_slots": total,
        "email": email,
        "email_present": present,
        "key_preview": _compact_key_preview(
            str(status.get("current_text_key_preview") or status.get("current_key_preview") or "")
        ),
        "status": "online" if slot_int and total else "warning",
    }


def _build_internet_identity(env: Mapping[str, str]) -> dict[str, Any]:
    configured = 0
    first_slot = None
    for slot in range(1, 5):
        if _env(f"GEMINI_INTERNET_API_KEY_{slot}", env=env):
            configured += 1
            if first_slot is None:
                first_slot = slot
    active_slot = first_slot
    try:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        state_path = os.path.join(root_dir, "configs", "internet_provider_state.json")
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            candidate = int(state.get("last_good_index")) if state.get("last_good_index") is not None else None
            if candidate and _env(f"GEMINI_INTERNET_API_KEY_{candidate}", env=env):
                active_slot = candidate
            elif bool(state.get("temporarily_unhealthy")) and state.get("unhealthy_index") is not None:
                unhealthy = int(state.get("unhealthy_index"))
                for slot in range(1, 5):
                    if slot != unhealthy and _env(f"GEMINI_INTERNET_API_KEY_{slot}", env=env):
                        active_slot = slot
                        break
    except Exception:
        active_slot = first_slot
    email, present = _email_or_placeholder(
        f"GEMINI_INTERNET_API_KEY_{active_slot}_EMAIL" if active_slot else "",
        env=env,
    )
    key_preview = _mask_secret(_env(f"GEMINI_INTERNET_API_KEY_{active_slot}", env=env)) if active_slot else ""
    return {
        "slot": active_slot,
        "active_slot": active_slot,
        "total": configured,
        "total_slots": 4,
        "configured_slots": configured,
        "email": email,
        "email_present": present,
        "key_preview": key_preview,
        "model": _env("GEMINI_INTERNET_MODEL", "gemini-2.5-flash", env=env) or "gemini-2.5-flash",
        "status": "standby" if configured else "warning",
    }


def create_api_identity_snapshot(
    *,
    provider: LiveKitProvider | None = None,
    key_report: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source = env if env is not None else os.environ
    livekit = _build_livekit_identity(source, provider)
    gemini = _build_gemini_identity(source, key_report)
    snapshot = {
        "ok": True,
        "livekit": livekit,
        "gemini": gemini,
        "internet": _build_internet_identity(source),
        "created_at": time.time(),
    }
    _store.snapshot = snapshot
    _store.created_at = float(snapshot["created_at"])
    livekit_slot_for_log = "local" if livekit.get("kind") == "local" else (livekit.get("slot") or "none")
    logger.info(
        "API_IDENTITY_SNAPSHOT_CREATED livekit=%s livekit_slot=%s gemini_slot=%s",
        livekit.get("provider_name") or livekit.get("provider") or "none",
        livekit_slot_for_log,
        gemini.get("slot") or "none",
    )
    logger.info(
        "API_IDENTITY_EMAIL_RESOLVED service=livekit slot=%s email_present=%s",
        livekit.get("slot") or "local",
        str(bool(livekit.get("email_present"))).lower(),
    )
    logger.info(
        "API_IDENTITY_EMAIL_RESOLVED service=gemini slot=%s email_present=%s",
        gemini.get("slot") or "none",
        str(bool(gemini.get("email_present"))).lower(),
    )
    return snapshot


def get_api_identity_snapshot(
    *,
    provider: LiveKitProvider | None = None,
    key_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if _store.snapshot is not None:
        return _store.snapshot
    return create_api_identity_snapshot(provider=provider, key_report=key_report)


def update_api_identity_snapshot(
    *,
    provider: LiveKitProvider | None = None,
    key_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return create_api_identity_snapshot(provider=provider, key_report=key_report)
