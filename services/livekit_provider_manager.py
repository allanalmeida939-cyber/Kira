from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit


log = logging.getLogger("kira.app")

DEFAULT_LOCAL_URL = "ws://127.0.0.1:7880"
DEFAULT_LOCAL_API_KEY = "devkey"
DEFAULT_LOCAL_API_SECRET = "secret"
VALID_PROVIDER_MODES = {"auto", "cloud", "local"}
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / "configs" / "livekit_provider_state.json"
DEFAULT_UNHEALTHY_TTL_SECONDS = 900.0
DEFAULT_BLOCKED_CLOUD_HOSTS: set[str] = set()  # a conta 3 (teste-kgxscdz0) era bloqueada aqui; hoje é conta válida


@dataclass(frozen=True, slots=True)
class LiveKitProvider:
    name: str
    kind: str
    url: str
    api_key: str
    api_secret: str
    priority: int
    enabled: bool = True


class LiveKitProviderError(RuntimeError):
    pass


_selected_provider: LiveKitProvider | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_provider_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, object]:
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception as exc:
        log.warning("LIVEKIT_PROVIDER_STATE_LOAD_FAILED path=%s error=%s", path, exc)
    return {}


def _save_provider_state(payload: dict[str, object], path: Path = DEFAULT_STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            "LIVEKIT_PROVIDER_STATE_SAVED provider=%s kind=%s last_success_provider=%s",
            str(payload.get("last_success_provider") or payload.get("last_failure_provider") or "none"),
            str(payload.get("last_success_kind") or "unknown"),
            str(payload.get("last_success_provider") or "none"),
        )
    except Exception as exc:
        log.warning("LIVEKIT_PROVIDER_STATE_SAVE_FAILED path=%s error=%s", path, exc)


def _sanitize_reason(reason: str, provider: LiveKitProvider) -> str:
    safe = str(reason or "").strip() or "unknown"
    for secret in (provider.api_secret, provider.api_key):
        if secret:
            safe = safe.replace(secret, "***")
    return safe[:240]


def _classify_failure_reason(reason: str) -> str:
    text = str(reason or "").strip().lower()
    if any(fragment in text for fragment in ("clientconnectordnserror", "getaddrinfo", "name resolution", "dns", "err_name_not_resolved")):
        return "dns"
    if any(fragment in text for fragment in ("clientconnectorcertificateerror", "certificate", "cert", "ssl", "cert_verify_failed")):
        return "cert"
    if any(fragment in text for fragment in ("timeout", "timed out", "timeouterror")):
        return "timeout"
    if any(fragment in text for fragment in ("401", "403", "unauthorized", "forbidden", "permission", "invalid api", "auth")):
        return "auth"
    if "missing_config" in text:
        return "config"
    return "unknown"


def _unhealthy_ttl_seconds(env: Mapping[str, str]) -> float:
    try:
        return max(float(_getenv(env, "LIVEKIT_PROVIDER_UNHEALTHY_TTL_SECONDS", str(DEFAULT_UNHEALTHY_TTL_SECONDS))), 30.0)
    except Exception:
        return DEFAULT_UNHEALTHY_TTL_SECONDS


def _temporary_unhealthy(state: dict[str, object]) -> dict[str, object]:
    value = state.get("temporary_unhealthy")
    return dict(value) if isinstance(value, dict) else {}


def _prune_temporary_unhealthy(state: dict[str, object], *, now: float | None = None) -> dict[str, object]:
    current = time.time() if now is None else float(now)
    unhealthy = _temporary_unhealthy(state)
    pruned = {
        str(name): dict(data)
        for name, data in unhealthy.items()
        if isinstance(data, dict) and float(data.get("until_epoch") or 0.0) > current
    }
    if pruned == unhealthy:
        return state
    updated = dict(state)
    if pruned:
        updated["temporary_unhealthy"] = pruned
    else:
        updated.pop("temporary_unhealthy", None)
    return updated


def _active_unhealthy_reason(state: dict[str, object], provider: LiveKitProvider) -> tuple[str, float] | None:
    data = _temporary_unhealthy(state).get(provider.name)
    if not isinstance(data, dict):
        return None
    until_epoch = float(data.get("until_epoch") or 0.0)
    remaining = until_epoch - time.time()
    if remaining <= 0:
        return None
    return str(data.get("reason") or "unknown"), remaining


def _mark_temporary_unhealthy(
    state: dict[str, object],
    provider: LiveKitProvider,
    *,
    reason: str,
    detail: str,
    ttl_seconds: float,
) -> dict[str, object]:
    unhealthy = _temporary_unhealthy(state)
    unhealthy[provider.name] = {
        "reason": reason,
        "detail": detail[:240],
        "marked_at": _utc_now_iso(),
        "until_epoch": time.time() + float(ttl_seconds),
    }
    updated = dict(state)
    updated["temporary_unhealthy"] = unhealthy
    updated["last_failure_provider"] = provider.name
    updated["last_failure_reason"] = f"{provider.name}:{reason}"
    updated["last_checked_at"] = _utc_now_iso()
    log.warning("LIVEKIT_PROVIDER_MARKED_UNHEALTHY provider=%s reason=%s", provider.name, reason)
    log.warning("LIVEKIT_PROVIDER_TEMP_UNHEALTHY provider=%s reason=%s ttl_s=%s", provider.name, reason, int(ttl_seconds))
    return updated


def _provider_by_name(providers: list[LiveKitProvider], name: str | None) -> LiveKitProvider | None:
    wanted = str(name or "").strip()
    if not wanted:
        return None
    for provider in providers:
        if provider.name == wanted:
            return provider
    return None


def _ordered_clouds_from_state(clouds: list[LiveKitProvider], state: dict[str, object]) -> tuple[list[LiveKitProvider], str, str]:
    configured_clouds = [provider for provider in clouds if _configured(provider)]
    if not configured_clouds:
        return [], "none", "default"
    primary_slot = _getenv(os.environ, "LIVEKIT_CLOUD_PRIMARY_SLOT", "1")
    primary_name = f"cloud_{primary_slot}"
    primary = _provider_by_name(configured_clouds, primary_name)
    if primary is not None:
        configured_clouds = [primary, *[provider for provider in configured_clouds if provider.name != primary.name]]
    last_success_name = str(state.get("last_success_provider") or "").strip()
    last_success_kind = str(state.get("last_success_kind") or "").strip().lower()
    preferred = _provider_by_name(configured_clouds, last_success_name) if last_success_kind == "cloud" else None
    source = "state" if preferred is not None else "default"
    if preferred is None:
        preferred = configured_clouds[0]
    else:
        log.info("LIVEKIT_LAST_GOOD_PROVIDER_RESTORED provider=%s kind=cloud", preferred.name)
    start_index = configured_clouds.index(preferred)
    ordered = configured_clouds[start_index:] + configured_clouds[:start_index]
    return ordered, preferred.name, source


def _getenv(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or "").strip()


def normalize_provider_mode(value: str | None) -> str:
    mode = str(value or "auto").strip().lower() or "auto"
    if mode not in VALID_PROVIDER_MODES:
        log.warning("LIVEKIT_PROVIDER_MODE_INVALID value=%s fallback=auto", mode)
        return "auto"
    return mode


def _valid_ws_url(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    return parsed.scheme in {"ws", "wss"} and bool(parsed.netloc)


def _configured(provider: LiveKitProvider) -> bool:
    return bool(provider.url and provider.api_key and provider.api_secret and _valid_ws_url(provider.url))


def _blocked_cloud_hosts() -> set[str]:
    raw = str(os.getenv("LIVEKIT_PROVIDER_BLOCKED_HOSTS", "") or "").strip()
    if raw:
        return {item.strip().lower() for item in raw.split(",") if item.strip()}
    return set(DEFAULT_BLOCKED_CLOUD_HOSTS)


def _check_cloud_endpoint_preflight(provider: LiveKitProvider, timeout_seconds: float) -> tuple[bool, str]:
    parsed = urlsplit(str(provider.url or "").strip())
    host = str(parsed.hostname or "").strip()
    if not host:
        return False, "missing_host"
    if host.lower() in _blocked_cloud_hosts():
        return False, f"blocked_host:{host}"
    port = int(parsed.port or (443 if parsed.scheme == "wss" else 80))
    timeout = max(float(timeout_seconds), 0.5)
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        return False, f"dns:{exc.__class__.__name__}:{str(exc)[:160]}"
    if parsed.scheme != "wss":
        return True, "endpoint_preflight_ok"
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            raw_sock.settimeout(timeout)
            with context.wrap_socket(raw_sock, server_hostname=host):
                return True, "endpoint_tls_ok"
    except ssl.SSLError as exc:
        return False, f"cert:{exc.__class__.__name__}:{str(exc)[:160]}"
    except Exception as exc:
        return False, f"connect:{exc.__class__.__name__}:{str(exc)[:160]}"


def _cloud_provider(env: Mapping[str, str], slot: int) -> LiveKitProvider:
    return LiveKitProvider(
        name=f"cloud_{slot}",
        kind="cloud",
        url=_getenv(env, f"LIVEKIT_CLOUD_URL_{slot}"),
        api_key=_getenv(env, f"LIVEKIT_CLOUD_API_KEY_{slot}"),
        api_secret=_getenv(env, f"LIVEKIT_CLOUD_API_SECRET_{slot}"),
        priority=slot,
        enabled=_getenv(env, f"LIVEKIT_CLOUD_ENABLED_{slot}", "true").lower() not in {"0", "false", "no", "off"},
    )


def _local_provider(env: Mapping[str, str]) -> LiveKitProvider:
    return LiveKitProvider(
        name="local",
        kind="local",
        url=_getenv(env, "LIVEKIT_LOCAL_URL") or _getenv(env, "LIVEKIT_URL") or DEFAULT_LOCAL_URL,
        api_key=_getenv(env, "LIVEKIT_LOCAL_API_KEY") or _getenv(env, "LIVEKIT_API_KEY") or DEFAULT_LOCAL_API_KEY,
        api_secret=_getenv(env, "LIVEKIT_LOCAL_API_SECRET") or _getenv(env, "LIVEKIT_API_SECRET") or DEFAULT_LOCAL_API_SECRET,
        priority=99,
        enabled=_getenv(env, "LIVEKIT_LOCAL_ENABLED", "true").lower() not in {"0", "false", "no", "off"},
    )


def build_provider_candidates(
    env: Mapping[str, str] | None = None,
    *,
    mode: str | None = None,
) -> list[LiveKitProvider]:
    source = env if env is not None else os.environ
    provider_mode = normalize_provider_mode(mode if mode is not None else _getenv(source, "LIVEKIT_PROVIDER", "auto"))
    clouds = [provider for provider in (_cloud_provider(source, slot) for slot in range(1, 10)) if provider.enabled]
    local = _local_provider(source)
    if provider_mode == "cloud":
        return clouds
    if provider_mode == "local":
        return [local] if local.enabled else []
    # AUTO é Cloud-first. O local só entra como fallback depois de uma falha
    # classificável no Cloud, nunca como escolha primária silenciosa.
    return [*clouds, *([local] if local.enabled else [])]


def build_livekit_provider_status(
    env: Mapping[str, str] | None = None,
    *,
    selected: LiveKitProvider | None = None,
) -> dict[str, object]:
    source = env if env is not None else os.environ
    local = _local_provider(source)
    clouds = [_cloud_provider(source, slot) for slot in (1, 2, 3)]
    selected_name = selected.name if selected is not None else ""
    return {
        "ok": True,
        "default_provider": "cloud",
        "prefer_local": False,
        "local_url": local.url or DEFAULT_LOCAL_URL,
        "cloud_enabled": any(_configured(provider) and provider.enabled for provider in clouds),
        "cloud_fallback_enabled": True,
        "cloud_fallback_mode": "cloud_first_then_local",
        "active_provider": selected_name,
        "active_kind": selected.kind if selected is not None else "",
        "local": {
            "name": local.name,
            "kind": local.kind,
            "url": local.url,
            "configured": _configured(local),
            "enabled": bool(local.enabled),
            "default": True,
            "active": selected_name == local.name,
        },
        "cloud": [
            {
                "name": provider.name,
                "kind": provider.kind,
                "url": provider.url,
                "configured": _configured(provider),
                "enabled": bool(provider.enabled),
                "reserve": provider.name != selected_name,
                "disabled_by_default": False,
                "active": selected_name == provider.name,
            }
            for provider in clouds
            if _configured(provider)
        ],
    }


def mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return "***ausente***"
    if len(text) <= 8:
        return "***presente***"
    return f"{text[:4]}...{text[-4:]}"


def mask_provider_for_logs(provider: LiveKitProvider | None) -> dict[str, object]:
    if provider is None:
        return {"selected": False}
    return {
        "selected": True,
        "name": provider.name,
        "kind": provider.kind,
        "url": provider.url,
        "api_key": mask_secret(provider.api_key),
        "api_secret": "***presente***" if provider.api_secret else "***ausente***",
        "priority": provider.priority,
    }


def _client_join_rejected(provider: LiveKitProvider) -> str:
    """'' se a conta aceita a tela entrar; senão o motivo (ex.: minutos esgotados). Falha de rede não conta como recusa."""
    import urllib.error
    import urllib.parse
    import urllib.request

    from livekit import api

    token = (api.AccessToken(provider.api_key, provider.api_secret).with_identity("kira-probe")
             .with_grants(api.VideoGrants(room_join=True, room="kira_probe")).to_jwt())
    base = provider.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1).rstrip("/")
    try:
        urllib.request.urlopen(f"{base}/rtc/validate?access_token={urllib.parse.quote(token)}", timeout=6).close()
        return ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            return f"{exc.code}:{exc.read()[:120].decode('utf-8', 'replace')}"
        return ""
    except Exception:
        return ""


async def _check_cloud_provider(provider: LiveKitProvider, timeout_seconds: float) -> tuple[bool, str]:
    preflight_ok, preflight_reason = _check_cloud_endpoint_preflight(provider, timeout_seconds)
    if not preflight_ok:
        return False, preflight_reason
    try:
        from livekit import api
    except Exception as exc:
        return False, f"livekit_api_unavailable:{exc.__class__.__name__}"

    client = None
    try:
        client = api.LiveKitAPI(
            url=provider.url,
            api_key=provider.api_key,
            api_secret=provider.api_secret,
        )
        request = api.ListRoomsRequest()
        await asyncio.wait_for(client.room.list_rooms(request), timeout=max(float(timeout_seconds), 0.5))
        # A API responde mesmo com os minutos grátis do mês esgotados, mas a tela é recusada (429 "connection minutes
        # limit exceeded" -> "could not establish signal connection"). Pergunta como a tela entraria.
        rejected = await asyncio.wait_for(asyncio.to_thread(_client_join_rejected, provider), timeout=max(float(timeout_seconds), 0.5) + 2)
        if rejected:
            return False, f"client_join_rejected:{rejected}"
        return True, "api_ok"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{str(exc)[:160]}"
    finally:
        close = getattr(client, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass


def _default_checker(provider: LiveKitProvider, timeout_seconds: float) -> tuple[bool, str]:
    if provider.kind == "local":
        return True, "local_deferred_to_server_check"
    try:
        return asyncio.run(_check_cloud_provider(provider, timeout_seconds))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_check_cloud_provider(provider, timeout_seconds))
        finally:
            loop.close()


def select_livekit_provider(
    env: Mapping[str, str] | None = None,
    *,
    mode: str | None = None,
    timeout_seconds: float | None = None,
    validate_cloud: bool = True,
    checker: Callable[[LiveKitProvider, float], tuple[bool, str]] | None = None,
) -> LiveKitProvider:
    global _selected_provider
    source = env if env is not None else os.environ
    provider_mode = normalize_provider_mode(mode if mode is not None else _getenv(source, "LIVEKIT_PROVIDER", "auto"))
    timeout = max(float(timeout_seconds if timeout_seconds is not None else _getenv(source, "LIVEKIT_PROVIDER_CHECK_TIMEOUT", "4.0")), 0.5)
    all_candidates = build_provider_candidates(source, mode=provider_mode)
    clouds = [provider for provider in all_candidates if provider.kind == "cloud"]
    local = next((provider for provider in all_candidates if provider.kind == "local"), None)
    state = _prune_temporary_unhealthy(_load_provider_state())
    unhealthy_ttl = _unhealthy_ttl_seconds(source)
    ordered_clouds, preferred_name, preferred_source = _ordered_clouds_from_state(clouds, state)
    first_cloud_retry = ordered_clouds[0] if ordered_clouds else None
    if provider_mode == "cloud":
        candidates = [*ordered_clouds, *([first_cloud_retry] if first_cloud_retry is not None else [])]
    elif provider_mode == "local":
        candidates = [local] if local is not None else []
    else:
        candidates = [*ordered_clouds, *([local] if local is not None else [])]
    cloud_failures: list[str] = []
    selected_checker = checker or _default_checker
    has_configured_cloud = bool(ordered_clouds)

    log.info("LIVEKIT_PROVIDER_SELECT_BEGIN mode=%s", provider_mode)
    if provider_mode == "auto":
        log.info("LIVEKIT_PROVIDER_DEFAULT_CLOUD_FIRST")
        log.info("LIVEKIT_CLOUD_PRIMARY_SLOT slot=%s", _getenv(source, "LIVEKIT_CLOUD_PRIMARY_SLOT", "1"))
        log.info("LIVEKIT_LOCAL_FALLBACK_AVAILABLE enabled=%s", str(local is not None).lower())
    log.info("LIVEKIT_CLOUD_PRIORITY_KEEP mode=%s cloud_count=%s local_enabled=%s", provider_mode, len(ordered_clouds), str(local is not None).lower())
    log.info("LIVEKIT_PROVIDER_BOOT_SELECTED provider=%s source=%s", preferred_name, preferred_source)
    log.info(
        "LIVEKIT_PROVIDER_ORDER providers=%s preferred=%s source=%s",
        ",".join(provider.name for provider in candidates) or "none",
        preferred_name,
        preferred_source,
    )
    seen_cloud_attempts = 0
    for index, candidate in enumerate(candidates):
        if candidate is None:
            continue
        configured = _configured(candidate)
        retry_first_cloud = (
            candidate.kind == "cloud"
            and first_cloud_retry is not None
            and candidate.name == first_cloud_retry.name
            and seen_cloud_attempts >= len(ordered_clouds)
        )
        if retry_first_cloud:
            log.info("LIVEKIT_PROVIDER_RETRY_FIRST_CLOUD name=%s reason=all_clouds_failed", candidate.name)
            log.info("LIVEKIT_PROVIDER_RETRY_FIRST_CLOUD_ONCE provider=%s", candidate.name)
        started_at = time.monotonic()
        log.info("LIVEKIT_PROVIDER_TRY name=%s kind=%s url=%s", candidate.name, candidate.kind, candidate.url)
        log.info("LIVEKIT_PROVIDER_CONNECT_BEGIN provider=%s kind=%s url=%s", candidate.name, candidate.kind, candidate.url)
        unhealthy = _active_unhealthy_reason(state, candidate)
        # com teste disponível, testa de novo (é rápido): a marca de 15 min pulava conta boa e caía no local à toa
        if candidate.kind == "cloud" and unhealthy is not None and not retry_first_cloud and not validate_cloud:
            reason, remaining = unhealthy
            safe_reason = f"temporary_unhealthy:{reason}"
            log.warning("LIVEKIT_PROVIDER_TEMP_UNHEALTHY provider=%s reason=%s remaining_s=%s", candidate.name, reason, int(remaining))
            log.warning("LIVEKIT_PROVIDER_CONNECT_FAILED provider=%s reason=%s detail=%s", candidate.name, reason, safe_reason)
            cloud_failures.append(f"{candidate.name}:{safe_reason}")
            seen_cloud_attempts += 1
            next_candidate = next((item for item in candidates[index + 1:] if item is not None), None)
            if next_candidate is not None:
                log.info("LIVEKIT_PROVIDER_FALLBACK_NEXT from=%s to=%s", candidate.name, next_candidate.name)
            continue
        if not configured:
            safe_reason = "missing_config"
            reason_kind = _classify_failure_reason(safe_reason)
            log.warning("LIVEKIT_PROVIDER_CONNECT_FAILED provider=%s reason=%s detail=%s", candidate.name, reason_kind, safe_reason)
            log.warning("LIVEKIT_PROVIDER_FAILED name=%s kind=%s reason=missing_config", candidate.name, candidate.kind)
            if candidate.kind == "cloud":
                state = _mark_temporary_unhealthy(
                    state,
                    candidate,
                    reason=reason_kind,
                    detail=safe_reason,
                    ttl_seconds=unhealthy_ttl,
                )
                _save_provider_state(state)
                cloud_failures.append(f"{candidate.name}:missing_config")
                seen_cloud_attempts += 1
                next_candidate = next((item for item in candidates[index + 1:] if item is not None), None)
                if next_candidate is not None:
                    log.info("LIVEKIT_PROVIDER_FALLBACK_NEXT from=%s to=%s", candidate.name, next_candidate.name)
            continue
        if candidate.kind == "cloud" and validate_cloud:
            ok, reason = selected_checker(candidate, timeout)
            if not ok:
                safe_reason = _sanitize_reason(reason, candidate)
                reason_kind = _classify_failure_reason(safe_reason)
                log.warning("LIVEKIT_PROVIDER_CONNECT_FAILED provider=%s reason=%s detail=%s", candidate.name, reason_kind, safe_reason)
                log.warning("LIVEKIT_PROVIDER_CHECK_FAILED name=%s reason=%s", candidate.name, safe_reason)
                log.warning("LIVEKIT_PROVIDER_FAILED name=%s kind=%s reason=%s", candidate.name, candidate.kind, safe_reason)
                state = _mark_temporary_unhealthy(
                    state,
                    candidate,
                    reason=reason_kind,
                    detail=safe_reason,
                    ttl_seconds=unhealthy_ttl,
                )
                _save_provider_state(state)
                cloud_failures.append(f"{candidate.name}:{safe_reason}")
                seen_cloud_attempts += 1
                next_candidate = next((item for item in candidates[index + 1:] if item is not None), None)
                if next_candidate is not None:
                    log.info("LIVEKIT_PROVIDER_FALLBACK_NEXT from=%s to=%s", candidate.name, next_candidate.name)
                continue
        if candidate.kind == "local" and (cloud_failures or has_configured_cloud) and provider_mode == "auto":
            log.info("LIVEKIT_PROVIDER_FALLBACK_LOCAL reason=all_clouds_failed_and_first_retry_failed")
            log.info("LIVEKIT_PROVIDER_LOCAL_FALLBACK_USED reason=all_clouds_failed")
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        log.info("LIVEKIT_PROVIDER_OK name=%s kind=%s elapsed_ms=%s", candidate.name, candidate.kind, elapsed_ms)
        log.info("LIVEKIT_PROVIDER_SELECTED name=%s kind=%s url=%s", candidate.name, candidate.kind, candidate.url)
        log.info("LIVEKIT_PROVIDER_CONNECT_OK provider=%s kind=%s elapsed_ms=%s", candidate.name, candidate.kind, elapsed_ms)
        next_cloud = candidate.name if candidate.kind == "cloud" else (first_cloud_retry.name if first_cloud_retry else None)
        new_state = dict(state)
        new_state.update(
            {
                "default_provider": "cloud",
                "prefer_local": False,
                "cloud_enabled": bool(ordered_clouds),
                "cloud_fallback_enabled": True,
                "cloud_fallback_mode": "cloud_first_then_local",
                "local_url": local.url if local is not None else DEFAULT_LOCAL_URL,
                "last_checked_at": _utc_now_iso(),
                "last_failure_provider": None,
                "last_failure_reason": None,
                "next_cloud_provider": next_cloud,
                "last_success_url": candidate.url,
                "local_used_as_fallback": bool(candidate.kind == "local" and has_configured_cloud),
            }
        )
        new_state["last_success_provider"] = candidate.name
        new_state["last_success_kind"] = candidate.kind
        _save_provider_state(new_state)
        _selected_provider = candidate
        return candidate

    if provider_mode == "cloud":
        raise LiveKitProviderError(
            "LiveKit Cloud indisponivel. Configure uma chave valida ou use LIVEKIT_PROVIDER=auto/local."
        )
    failure = cloud_failures[-1] if cloud_failures else "no_provider_configured"
    new_state = dict(state)
    new_state.update(
        {
            "last_checked_at": _utc_now_iso(),
            "last_failure_provider": failure.split(":", 1)[0],
            "last_failure_reason": failure,
            "local_used_as_fallback": False,
        }
    )
    _save_provider_state(new_state)
    raise LiveKitProviderError("Nenhum provider LiveKit configurado.")


def apply_provider_to_env(provider: LiveKitProvider, env: dict[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    target["LIVEKIT_URL"] = provider.url
    target["LIVEKIT_API_KEY"] = provider.api_key
    target["LIVEKIT_API_SECRET"] = provider.api_secret
    target["LIVEKIT_PROVIDER_SELECTED"] = provider.name
    target["LIVEKIT_PROVIDER_NAME"] = provider.name
    target["LIVEKIT_PROVIDER_KIND"] = provider.kind


def get_livekit_provider(
    env: Mapping[str, str] | None = None,
    *,
    force_refresh: bool = False,
    **kwargs,
) -> LiveKitProvider:
    global _selected_provider
    if _selected_provider is not None and not force_refresh:
        return _selected_provider
    _selected_provider = select_livekit_provider(env, **kwargs)
    return _selected_provider


def get_selected_provider() -> LiveKitProvider | None:
    return _selected_provider


def is_cloud_provider(provider: LiveKitProvider | None = None) -> bool:
    target = provider if provider is not None else _selected_provider
    return bool(target and target.kind == "cloud")


def is_local_provider(provider: LiveKitProvider | None = None) -> bool:
    target = provider if provider is not None else _selected_provider
    return bool(target and target.kind == "local")


def get_livekit_url() -> str:
    return get_livekit_provider().url


def get_livekit_api_key() -> str:
    return get_livekit_provider().api_key


def get_livekit_api_secret() -> str:
    return get_livekit_provider().api_secret
